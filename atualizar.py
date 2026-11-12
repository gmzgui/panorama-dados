"""
Panorama — dados fundamentalistas a partir da CVM (Dados Abertos)
------------------------------------------------------------------
Baixa os formulários oficiais das companhias abertas (DFP anual e ITR trimestral),
extrai os números das empresas listadas em ativos.txt e grava um resumo por ticker
em dados/<TICKER>.json. Roda sozinho toda semana pelo GitHub Actions.

Fontes (Portal de Dados Abertos da CVM):
  FCA  (ticker → CNPJ):  /dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_<ano>.zip
  ITR  (trimestral):     /dados/CIA_ABERTA/DOC/ITR/DADOS/itr_cia_aberta_<ano>.zip
  DFP  (anual):          /dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_<ano>.zip

Uso:  python atualizar.py            (anos padrão: últimos 6)
      python atualizar.py --anos 3
"""
import csv, io, json, os, re, sys, unicodedata, urllib.request, zipfile
from collections import defaultdict
from datetime import date, datetime, timezone

BASE = os.environ.get("CVM_BASE", "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC")
PASTA = os.path.dirname(os.path.abspath(__file__))
SAIDA = os.path.join(PASTA, "dados")
CACHE = os.path.join(PASTA, ".cache")

# Quantas ações formam uma unit (ticker terminado em 11 que não é FII/ETF).
UNITS_PADRAO = {"BPAC11": 3, "SAPR11": 5, "TAEE11": 3, "KLBN11": 5, "SANB11": 2, "ALUP11": 3, "ENGI11": 5}

csv.field_size_limit(10_000_000)


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return s.strip().lower()


def num(v):
    """VL_CONTA vem como '2398719197.0000000000' ou '1.234,56'."""
    s = str(v or "").strip()
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def baixar(url):
    """Baixa com cache local (o Actions começa sempre limpo; localmente evita baixar de novo)."""
    os.makedirs(CACHE, exist_ok=True)
    destino = os.path.join(CACHE, url.rsplit("/", 1)[-1])
    if os.path.exists(destino) and os.path.getsize(destino) > 0:
        return destino
    print(f"  baixando {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "panorama-dados (uso pessoal)"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r, open(destino, "wb") as f:
            while True:
                bloco = r.read(1 << 20)
                if not bloco:
                    break
                f.write(bloco)
    except Exception as e:
        print(f"  ! não foi possível baixar {url}: {e}", flush=True)
        if os.path.exists(destino):
            os.remove(destino)
        return None
    return destino


def linhas_csv(zip_path, padrao):
    """Itera as linhas (dict) do primeiro CSV do zip cujo nome casa com o padrão."""
    with zipfile.ZipFile(zip_path) as z:
        nomes = [n for n in z.namelist() if re.search(padrao, n, re.I)]
        if not nomes:
            return
        with z.open(nomes[0]) as bruto:
            texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
            leitor = csv.DictReader(texto, delimiter=";")
            for linha in leitor:
                yield {k.strip(): (v or "").strip() for k, v in linha.items() if k}


def col(linha, *trechos):
    """Acha a coluna cujo nome contém todos os trechos (robusto a mudanças de caixa)."""
    for k in linha:
        nk = norm(k)
        if all(t in nk for t in trechos):
            return k
    return None


def so_digitos(s):
    return re.sub(r"\D", "", s or "")


# ---------------------------------------------------------------- ativos.txt
def ler_ativos():
    ativos, units = [], dict(UNITS_PADRAO)
    caminho = os.path.join(PASTA, "ativos.txt")
    with open(caminho, encoding="utf-8") as f:
        for bruto in f:
            linha = bruto.split("#", 1)[0].strip()
            if not linha:
                continue
            partes = linha.replace(";", " ").replace(",", " ").split()
            t = partes[0].upper()
            ativos.append(t)
            for p in partes[1:]:
                if p.isdigit():
                    units[t] = int(p)
                elif re.fullmatch(r"\d{14}", so_digitos(p)):
                    units.setdefault("__cnpj__" + t, so_digitos(p))
    return ativos, units


# ---------------------------------------------------------------- ticker → CNPJ
def mapa_tickers(anos, ativos, extras):
    alvo = set(ativos)
    mapa = {t: extras["__cnpj__" + t] for t in ativos if "__cnpj__" + t in extras}
    for ano in sorted(anos, reverse=True):
        if alvo <= set(mapa):
            break
        z = baixar(f"{BASE}/FCA/DADOS/fca_cia_aberta_{ano}.zip")
        if not z:
            continue
        ck = cn = None
        for l in linhas_csv(z, r"valor_mobiliario"):
            if ck is None:
                ck, cn = col(l, "negocia"), col(l, "cnpj")
                if not ck or not cn:
                    print("  ! colunas de ticker/CNPJ não encontradas no FCA:", list(l)[:12])
                    break
            t = l.get(ck, "").upper()
            if t in alvo and t not in mapa:
                mapa[t] = so_digitos(l.get(cn))
    # Units e tickers sem cadastro próprio: tenta o mesmo prefixo (BPAC11 → BPAC3/BPAC5)
    return mapa


# ---------------------------------------------------------------- leitura dos formulários
def ultimo_versao(linhas_por_doc):
    """Mantém só a maior VERSAO de cada (CNPJ, DT_REFER)."""
    melhor = {}
    for (cnpj, ref, versao) in linhas_por_doc:
        k = (cnpj, ref)
        if k not in melhor or versao > melhor[k]:
            melhor[k] = versao
    return melhor


def ler_formulario(zip_path, prefixo, cnpjs):
    """Lê DRE/BPA/BPP (consolidado; individual se não houver) e composição do capital."""
    out = {"DRE": [], "BPA": [], "BPP": [], "CAP": []}
    for chave, padrao in (("DRE", r"_DRE_con_"), ("BPA", r"_BPA_con_"), ("BPP", r"_BPP_con_")):
        achou = set()
        for l in linhas_csv(zip_path, padrao) or []:
            c = so_digitos(l.get("CNPJ_CIA"))
            if c in cnpjs:
                out[chave].append(l)
                achou.add(c)
        faltam = cnpjs - achou
        if faltam:  # empresas que só publicam o individual
            for l in linhas_csv(zip_path, padrao.replace("_con_", "_ind_")) or []:
                c = so_digitos(l.get("CNPJ_CIA"))
                if c in faltam:
                    l["_individual"] = "1"
                    out[chave].append(l)
    for l in linhas_csv(zip_path, r"composicao_capital") or []:
        if so_digitos(l.get("CNPJ_CIA")) in cnpjs:
            out["CAP"].append(l)
    return out


def escala(l):
    return 1000.0 if norm(l.get("ESCALA_MOEDA")) == "mil" else 1.0


def meses(ini, fim):
    a, b = date.fromisoformat(ini), date.fromisoformat(fim)
    return (b.year - a.year) * 12 + (b.month - a.month) + 1


def filtra_ultima_versao(linhas):
    versoes = ultimo_versao([(so_digitos(l.get("CNPJ_CIA")), l.get("DT_REFER"), int(l.get("VERSAO") or 0)) for l in linhas])
    return [l for l in linhas if versoes.get((so_digitos(l.get("CNPJ_CIA")), l.get("DT_REFER"))) == int(l.get("VERSAO") or 0)]


# ---------------------------------------------------------------- identificação das contas
def tipo_empresa(dre):
    for l in dre:
        if l.get("CD_CONTA") == "3.01":
            d = norm(l.get("DS_CONTA"))
            if "intermedia" in d:
                return "banco"
            if "premio" in d or "seguro" in d or "previdencia" in d:
                return "seguradora"
            return "geral"
    return "geral"


def nivel(cd):
    return cd.count(".")


def contas_dre(cd, ds):
    """Devolve o(s) nome(s) padronizado(s) desta conta da DRE."""
    d = norm(ds)
    nomes = []
    if cd == "3.01":
        nomes.append("receita")
    if nivel(cd) == 1 and "resultado bruto" in d:
        nomes.append("resultado_bruto")
    if nivel(cd) == 1 and "antes do resultado financeiro" in d:
        nomes.append("ebit")
    if nivel(cd) == 1 and ("lucro" in d or "prejuizo" in d) and "periodo" in d and ("consolidado" in d or "liquido" in d):
        nomes.append("lucro_liquido")
    if nivel(cd) == 2 and "controladora" in d and "nao" not in d and cd.startswith("3."):
        nomes.append("lucro_controladores")
    return nomes


# ---------------------------------------------------------------- montagem por empresa
def montar(cnpj, dfps, itrs):
    """dfps/itrs: listas de dicts {DRE,BPA,BPP,CAP} já filtrados por versão."""
    # valores de DRE por (conta, inicio, fim) — só ÚLTIMO exercício
    dre = defaultdict(dict)
    todas_dre = []
    for bloco in dfps + itrs:
        for l in bloco["DRE"]:
            if so_digitos(l.get("CNPJ_CIA")) != cnpj or not norm(l.get("ORDEM_EXERC")).startswith("ultimo"):
                continue
            todas_dre.append(l)
            ini, fim = l.get("DT_INI_EXERC"), l.get("DT_FIM_EXERC")
            if not ini or not fim:
                continue
            v = num(l.get("VL_CONTA"))
            if v is None:
                continue
            for nome in contas_dre(l.get("CD_CONTA", ""), l.get("DS_CONTA")):
                # se a mesma conta padronizada aparecer duas vezes no mesmo período, fica a 1ª (nível mais alto)
                dre[(nome, ini, fim)].setdefault("v", v * escala(l))
    tipo = tipo_empresa(todas_dre)

    # trimestres: valores de 3 meses; se faltar, deriva de acumulados (YTD)
    fins = sorted({fim for (_, ini, fim) in dre})
    nomes = ["receita", "resultado_bruto", "ebit", "lucro_liquido", "lucro_controladores"]
    trimestres = []
    for fim in fins:
        f = date.fromisoformat(fim)
        if f.month not in (3, 6, 9, 12):
            continue
        ini_tri = date(f.year, f.month - 2, 1).isoformat()
        ini_ano = date(f.year, 1, 1).isoformat()
        linha = {"fim": fim}
        tem = False
        for n in nomes:
            v = dre.get((n, ini_tri, fim), {}).get("v")
            if v is None:
                ytd = dre.get((n, ini_ano, fim), {}).get("v")
                if ytd is not None and f.month == 3:
                    v = ytd
                elif ytd is not None:
                    # acumulado até o trimestre anterior
                    ant = None
                    for (nn, ii, ff), dd in dre.items():
                        if nn == n and ii == ini_ano and ff < fim and date.fromisoformat(ff).month == f.month - 3:
                            ant = dd["v"]
                    if ant is not None:
                        v = ytd - ant
                    else:
                        # sem o acumulado anterior: acumulado do ano − trimestres já calculados do mesmo ano
                        anteriores = [t.get(n) for t in trimestres if t["fim"][:4] == fim[:4] and t["fim"] < fim]
                        if len(anteriores) == f.month // 3 - 1 and all(x is not None for x in anteriores):
                            v = ytd - sum(anteriores)
            if v is not None:
                tem = True
            linha[n] = v
        if tem:
            trimestres.append(linha)

    # balanço por data de referência (ÚLTIMO)
    bal = defaultdict(dict)
    for bloco in dfps + itrs:
        for chave in ("BPA", "BPP"):
            for l in bloco[chave]:
                if so_digitos(l.get("CNPJ_CIA")) != cnpj or not norm(l.get("ORDEM_EXERC")).startswith("ultimo"):
                    continue
                ref = l.get("DT_FIM_EXERC") or l.get("DT_REFER")
                cd, d = l.get("CD_CONTA", ""), norm(l.get("DS_CONTA"))
                v = num(l.get("VL_CONTA"))
                if v is None:
                    continue
                v *= escala(l)
                b = bal[ref]
                if cd == "1":
                    b["ativo_total"] = v
                elif cd == "1.01.01":
                    b["caixa"] = b.get("caixa", 0) + v
                elif cd == "1.01.02" and "aplica" in d:
                    b["caixa"] = b.get("caixa", 0) + v
                elif nivel(cd) == 1 and cd.startswith("2.") and "patrimonio liquido" in d:
                    b["pl"] = v
                elif nivel(cd) == 2 and cd.startswith("2.0") and "nao controlador" in d:
                    b["minoritarios"] = v
                elif nivel(cd) == 2 and cd.startswith(("2.01.", "2.02.")) and "emprestimos e financiamentos" in d:
                    b["divida_bruta"] = b.get("divida_bruta", 0) + v
    balancos = []
    for ref in sorted(bal):
        b = bal[ref]
        if "pl" not in b and "ativo_total" not in b:
            continue
        pl = b.get("pl")
        item = {"data": ref, "ativo_total": b.get("ativo_total"), "pl": pl,
                "pl_controladores": (pl - b.get("minoritarios", 0)) if pl is not None else None}
        if tipo == "geral":
            item["caixa"] = b.get("caixa")
            item["divida_bruta"] = b.get("divida_bruta", 0.0)
            item["divida_liquida"] = item["divida_bruta"] - (b.get("caixa") or 0)
        balancos.append(item)

    # ações (composição do capital mais recente)
    acoes = None
    caps = [l for bloco in itrs + dfps for l in bloco["CAP"] if so_digitos(l.get("CNPJ_CIA")) == cnpj]
    if caps:
        caps.sort(key=lambda l: (l.get("DT_REFER", ""), int(l.get("VERSAO") or 0)))
        l = caps[-1]
        tot, tes = col(l, "total", "integr"), col(l, "total", "tesour")
        on, pn = col(l, "ordin", "integr"), col(l, "pref", "integr")
        total = num(l.get(tot)) if tot else None
        if total is None and on:
            total = (num(l.get(on)) or 0) + (num(l.get(pn)) or 0 if pn else 0)
        tesouraria = num(l.get(tes)) if tes else 0
        if total:
            acoes = {"data": l.get("DT_REFER"), "total": total, "tesouraria": tesouraria or 0,
                     "em_circulacao": total - (tesouraria or 0),
                     "ordinarias": num(l.get(on)) if on else None, "preferenciais": num(l.get(pn)) if pn else None}

    denom = next((l.get("DENOM_CIA") for l in todas_dre if l.get("DENOM_CIA")), None)
    cd_cvm = next((l.get("CD_CVM") for l in todas_dre if l.get("CD_CVM")), None)
    individual = any(l.get("_individual") for l in todas_dre)
    return {"empresa": denom, "cnpj": cnpj, "cd_cvm": cd_cvm, "tipo": tipo, "demonstracoes": "individual" if individual else "consolidado",
            "trimestres": trimestres[-24:], "balancos": balancos[-12:], "acoes": acoes}


def ttm(trimestres):
    ult = trimestres[-4:]
    if len(ult) < 4:
        return None
    out = {"periodo": f"{ult[0]['fim'][:7]} a {ult[-1]['fim'][:7]}"}
    for n in ("receita", "resultado_bruto", "ebit", "lucro_liquido", "lucro_controladores"):
        vals = [t.get(n) for t in ult]
        out[n] = sum(vals) if all(v is not None for v in vals) else None
    return out


# ---------------------------------------------------------------- principal
def main():
    anos_n = 6
    if "--anos" in sys.argv:
        anos_n = int(sys.argv[sys.argv.index("--anos") + 1])
    hoje = date.today()
    anos = list(range(hoje.year - anos_n + 1, hoje.year + 1))
    ativos, extras = ler_ativos()
    print(f"Ativos: {', '.join(ativos)}")

    print("1/3 Identificando empresas (FCA)…")
    mapa = mapa_tickers(anos, ativos, extras)
    # units/tickers sem código próprio no FCA: tenta outra classe do mesmo emissor
    if set(ativos) - set(mapa):
        for ano in sorted(anos, reverse=True):
            z = baixar(f"{BASE}/FCA/DADOS/fca_cia_aberta_{ano}.zip")
            if not z:
                continue
            for l in linhas_csv(z, r"valor_mobiliario"):
                ck, cn = col(l, "negocia"), col(l, "cnpj")
                t = l.get(ck, "").upper() if ck else ""
                for alvo in set(ativos) - set(mapa):
                    if t[:4] == alvo[:4] and t != alvo:
                        mapa[alvo] = so_digitos(l.get(cn))
            break
    for t in ativos:
        print(f"  {t}: {'CNPJ ' + mapa[t] if t in mapa else 'não encontrado na CVM (ETF, FII, BDR ou ticker errado)'}")
    cnpjs = set(mapa.values())
    if not cnpjs:
        print("Nenhuma empresa encontrada. Confira ativos.txt.")
        return 1

    print("2/3 Lendo DFP (anual) e ITR (trimestral)…")
    dfps, itrs = [], []
    for ano in anos:
        for tipo, lista in (("DFP", dfps), ("ITR", itrs)):
            z = baixar(f"{BASE}/{tipo}/DADOS/{tipo.lower()}_cia_aberta_{ano}.zip")
            if not z:
                continue
            bloco = ler_formulario(z, tipo, cnpjs)
            for k in ("DRE", "BPA", "BPP", "CAP"):
                bloco[k] = filtra_ultima_versao(bloco[k])
            lista.append(bloco)
            print(f"  {tipo} {ano}: {sum(len(v) for v in bloco.values())} linhas das empresas", flush=True)

    print("3/3 Gravando dados/…")
    os.makedirs(SAIDA, exist_ok=True)
    agora = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    indice = {"atualizado_em": agora, "fonte": "CVM — Portal de Dados Abertos (DFP/ITR/FCA)", "ativos": {}}
    for t in ativos:
        if t not in mapa:
            indice["ativos"][t] = {"status": "sem dados na CVM"}
            continue
        r = montar(mapa[t], dfps, itrs)
        r.update({"ticker": t, "acoes_por_unit": extras.get(t, 1), "ttm": ttm(r["trimestres"]),
                  "atualizado_em": agora, "fonte": "CVM — Portal de Dados Abertos (DFP/ITR)",
                  "unidade": "R$ (valores já convertidos da escala da CVM)"})
        with open(os.path.join(SAIDA, f"{t}.json"), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, separators=(",", ":"))
        ult = r["trimestres"][-1]["fim"] if r["trimestres"] else None
        indice["ativos"][t] = {"status": "ok", "empresa": r["empresa"], "ultimo_trimestre": ult, "trimestres": len(r["trimestres"])}
        print(f"  {t}: {r['empresa']} · {len(r['trimestres'])} trimestres · último {ult} · tipo {r['tipo']}")
    with open(os.path.join(SAIDA, "_indice.json"), "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, indent=1)
    print("Pronto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
