# Radar — eventos em tempo real em Portugal

Painel que se atualiza sozinho com **eventos naturais** (tempestades, cheias, incêndios, sismos, vulcões,
tsunamis, secas, calor e frio) e **catástrofes humanas** (guerras, terrorismo, ciberataques, pandemias e
acidentes nucleares), com mapas animados, alertas para Portugal e notícias. Pode ser mostrado na TV e
comandado pelo telemóvel.

> O Radar junta dados públicos para informação e **não substitui os avisos oficiais**.
> Em caso de perigo segue sempre o IPMA, a Proteção Civil (ANEPC) e a DGS.

## Instalar (Windows 10/11)

1. Descarrega o repositório (**Code → Download ZIP**) e extrai-o.
2. Faz duplo clique em **`instalar.bat`**. Ele:
   - instala o Python pelo `winget`, se faltar (os termos do Python aparecem no ecrã para aceitares);
   - instala as bibliotecas (`requirements.txt`: pywebview);
   - cria os atalhos **Radar** no menu Iniciar e no ambiente de trabalho;
   - pergunta, se quiseres, por uma chave gratuita da [Groq](https://console.groq.com/keys) para a IA.
3. Abre o Radar pelo atalho **Radar**.

Sem instalador: `pip install -r requirements.txt` e depois `python app.py`
(`python app.py --no-window` só abre o servidor, para usar no browser ou na TV).

## O que tem

- **Lista de eventos** com abas *Ao vivo* (alertas vermelhos) e *Todos*, 10 tipos (teclas 1–9 e 0) e pesquisa
  (com IA, se houver chave).
- **Alertas para Portugal** com regras estritas: vermelho só quando é extremamente grave para Portugal.
- **Mapas** próprios para cada tipo (trajetória das tempestades, caudal dos rios, áreas ardidas, ondas sísmicas,
  frentes de tsunami, território ocupado na Ucrânia com barra de tempo desde 2022, …) e fundos animados.
- **Na TV**: duplicar o ecrã, abrir no browser da TV, ou a app Android TV (`RadarCharles.apk`).
- **Comando**: telemóvel (código QR), teclado ou comando da TV.

## IA (opcional)

Com `groq_key.txt` (a tua chave, **não a partilhes**) o Radar usa o modelo GPT-OSS-120B da Groq para resumir as
guerras, reconhecer ataques e ciberataques nas notícias, traduzir os surtos da OMS e pesquisar em linguagem
natural. Sem chave, tudo o resto funciona.

## Fontes de dados

IPMA, Open-Meteo, Copernicus (GloFAS, EFFIS), NASA POWER, Fogos.pt, Proteção Civil (ANEPC), USGS,
Smithsonian GVP, NOAA (tsunamis), OMS, AIEA, CISA, DeepStateMap, Wikipédia e Google Notícias.
Mapas © Esri, HERE, Garmin, OpenStreetMap. Os dados pertencem às respetivas fontes.
