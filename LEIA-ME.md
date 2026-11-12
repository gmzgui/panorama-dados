# panorama-dados

Resumos fundamentalistas (balanços e resultados trimestrais) das empresas listadas em `ativos.txt`,
extraídos dos formulários oficiais da CVM (Portal de Dados Abertos: DFP, ITR e FCA).

- `atualizar.py` — baixa os arquivos da CVM e grava `dados/<TICKER>.json`.
- `.github/workflows/atualizar.yml` — roda o script toda segunda-feira (6h de Brasília) e quando você clicar em **Run workflow**.
- `ativos.txt` — os tickers acompanhados. Edite pelo próprio GitHub para incluir ou remover empresas.

O conector Panorama lê os arquivos de `dados/` e calcula os múltiplos com o preço do dia.
Dados públicos da CVM; uso pessoal, não é recomendação de investimento.
