# Prospecção B2B Human-in-the-Loop — WhatsApp (Clínicas)

Script Python para prospecção **semi-supervisionada** de clínicas odontológicas e
médicas locais. Nenhum envio é 100% autônomo: **cada mensagem passa por revisão
e confirmação humana**, com limite diário, intervalo aleatório, janela de horário,
bloqueio de fim de semana e variação de texto (spintax) para reduzir o risco de
banimento de chip pela Meta.

## Arquivos

| Arquivo | Descrição |
|---|---|
| `prospeccao_whatsapp.py` | Script principal de envio (somente stdlib; Selenium opcional) |
| `mensagens.json` | Textos da abordagem e do follow-up (spintax + tokens) — edite aqui, não no código |
| `minerador_maps.py` | Minerador de empresas no Google Maps (Playwright) → `prospeccao.csv` |
| `prospeccao.csv` | Contatos (`clinica,bairro,cidade,telefone,site,avaliacoes,status,resposta,atualizado_em,ultimo_envio,followups`) |
| `test_prospeccao.py` + `test_minerador.py` | 139 testes do núcleo anti-ban, do modo automático e da mineração (unittest, zero dependências) |
| `logs/` | Log diário do modo automático (`automatico_AAAA-MM-DD.log`) |
| `termos_mineracao.txt` | Termos do rodízio semanal do minerador |
| `historico_envios.jsonl` | Auditoria: todo envio confirmado é registrado aqui |
| `requirements.txt` | `playwright`, `selenium` (opcional) |

## Como rodar

```bash
# 1. Instalação (uma vez)
pip install -r requirements.txt
playwright install chromium        # baixa o Chromium do Playwright

# 2. Minerar empresas do Google Maps -> prospeccao.csv (dedup por telefone)
python minerador_maps.py --termo "clinica odontologica em Aracaju SE" --max 40
python minerador_maps.py --termo "clinica medica em Aracaju SE" --max 30 --visivel
python minerador_maps.py --termo "dentista em Aracaju" --dry-run   # só visualizar
python minerador_maps.py --termo "dentista em Aracaju" --incluir-fixos

# 3. Fila de envio — prévia → sua confirmação → envio → intervalo 45–90s
python prospeccao_whatsapp.py

# 4. Acompanhamento
python prospeccao_whatsapp.py --respostas       # respondeu / sem resposta / opt-out
python prospeccao_whatsapp.py --followup        # retoma quem não respondeu há 3+ dias

# Outros comandos úteis
python prospeccao_whatsapp.py --listar          # lista contatos + respostas
python prospeccao_whatsapp.py --preview         # dry-run: só gera prévias
python prospeccao_whatsapp.py --contato 3       # começa a fila pelo 3º contato
python prospeccao_whatsapp.py --clinica ferraz  # filtra pelo nome da clínica
python prospeccao_whatsapp.py --modo web        # WhatsApp Web via Selenium
python prospeccao_whatsapp.py --reset           # reabre quem recebeu e não respondeu
python prospeccao_whatsapp.py --limite-dia 15 --hora-inicio 9 --hora-fim 18
python prospeccao_whatsapp.py --followup --dias-followup 5 --max-followups 2

# 5. Testes (biblioteca padrão, sem instalar nada)
python -m unittest discover -v
```

Requisitos: Python 3.10+. O modo `manual` (padrão) não precisa de nada instalado.

## Minerador Google Maps (`minerador_maps.py`)

- **Entrada**: `--termo` de busca configurável e `--max` (padrão: 40 alvos).
- **Extração por card**: `clinica` (h1), `bairro` (parser de endereço com 4
  formatos cobertos), `cidade`, `telefone` (higienizado com DDI 55 — reutiliza
  `normalizar_telefone`/`validar_telefone` do script de envio), `site`
  (`data-item-id="authority"`), `avaliacoes` (nota + volume) e `status=pendente`.
- **Só celulares por padrão**: telefones fixos raramente têm WhatsApp e são
  pulados; use `--incluir-fixos` para mantê-los.
- **Robustez/evasão básica**:
  - Seletores semânticos (`data-item-id`, `role`, `aria-label`, `href`) em vez
    de classes geradas pelo Google;
  - Rolagem suave incremental no `div[role="feed"]` lateral até o limite;
  - Pausas aleatórias de 1–3s (`--delay-min`/`--delay-max`) entre navegações;
  - Fallback campo a campo: um elemento que falha não derruba a mineração.
- **Saída**: integra direto no `prospeccao.csv` do script de envio —
  **nunca sobrescreve** registros existentes; duplicidade verificada por
  **telefone** (higienizado). Gravação atômica com a biblioteca padrão.
- **Aviso**: a mineração consulta dados públicos em baixo volume; respeite os
  Termos de Serviço do Google e mantenha as pausas — volume alto gera CAPTCHA
  e bloqueio. O script para automaticamente se a lista lateral não aparecer.

## Fluxo de execução (sempre semi-supervisionado)

1. Itera **apenas** sobre contatos com `status == 'pendente'` (ou, com
   `--followup`, sobre enviados sem resposta há N dias).
2. **Valida** telefone (55 + DDD + número), **ignora duplicatas** e bloqueia
   reenvio para o mesmo número no mesmo dia.
3. Monta o texto dinâmico a partir do `mensagens.json` (spintax + saudação
   conforme o horário + personalização por clínica/bairro/cidade).
4. Mostra a prévia: aprovar, gerar nova variação, pular, editar ou sair.
5. Abre `https://web.whatsapp.com/send?phone={telefone}&text={mensagem}`.
6. Você revisa e envia; o script só registra depois da sua confirmação.
7. Atualiza `status=enviado`, `ultimo_envio` (e `followups`) no CSV.
8. Antes do próximo envio, garante um intervalo aleatório de **45 a 90
   segundos** desde o último envio real. O tempo que você gasta revisando já
   conta, e pular contatos ou pedir nova variação não reinicia a espera.

## Proteções anti-ban

- **Nunca envia sozinho**: sem sua ação explícita (`s` / `ENTER` / digitar `ENVIAR`), nada sai.
- **Janela de horário**: 08h–19h por padrão (`--hora-inicio` / `--hora-fim`) — fora dela a fila é bloqueada.
- **Só dias úteis**: sábado e domingo são bloqueados (`--fim-de-semana` libera).
- **Limite diário**: padrão 20 mensagens/dia (`--limite-dia`), contando follow-ups.
- **Sem reenvio no mesmo dia**: consulta o histórico por número; se já recebeu hoje, exige confirmação explícita para forçar.
- **Intervalo mínimo**: 45–90s desde o último envio, com contagem regressiva.
- **Opt-out**: quem pediu para não ser contatado vira `nao_contatar` e nunca volta à fila — nem com `--reset`.
- **Deduplicação + validação de telefone** no carregamento do CSV.
- **Spintax**: saudação, gancho, autoridade, case e CTA variam a cada geração — nenhuma mensagem idêntica.
- **Auditoria**: cada envio confirmado vira uma linha em `historico_envios.jsonl` (com `tipo`: `inicial`/`followup`).
- **Ctrl+C seguro**: cada envio e cada resposta são gravados na hora.

## Personalizando os textos (`mensagens.json`)

- `saudacoes`: listas `manha` (5h–12h), `tarde` (12h–18h), `noite` e `qualquer`.
- `ganchos`, `autoridades`, `cases`, `ctas`: uma opção de cada é sorteada.
- `estruturas`: como as partes são montadas (`{saudacao}`, `{gancho}`, ...).
- `followups`: mensagens curtas de retomada.
- `aberturas`: variante curta do teste A/B; `estruturas_apresentacao` + `ctas_apresentacao`: 2ª etapa da curta, enviada quando uma pessoa responde.
- `estruturas_resumo`: 2ª etapa da completa — o resumo prometido no CTA ("te mando um resumo"), enviado quando uma pessoa responde.
- `versao`: identificador dos textos (ex.: `"2026-11-11"`). **Mude sempre que reescrever as mensagens**: ela é gravada na coluna `versao_textos` do CSV no 1º envio, e o resumo do teste A/B conta só os contatos da versão atual.
- Token `{nota}`: nota do Google (só existe se ≥ 4,5; textos com `{nota}` são usados só nesses casos).
- Tokens: `{clinica}`, `{bairro}`, `{cidade}` e `{local}` — que vira
  `no <bairro>` ou, quando o bairro está vazio, `em <cidade>`.
- Spintax em qualquer texto: `{Faz sentido|Vale a pena}`.

Para outro arquivo de textos: `--mensagens outro.json`.

## Preparando os dados

Edite `prospeccao.csv` mantendo o cabeçalho:

```csv
clinica,bairro,cidade,telefone,site,avaliacoes,status,resposta,atualizado_em,ultimo_envio,followups
Ferraz Odontologia,São José,Aracaju,5579999631871,,,pendente,,,,
```

- `telefone`: número internacional sem `+` (ex.: `5579999631871`). Números locais
  recebem o `55` automaticamente. Linhas com telefone inválido ou duplicado
  ficam **fora da fila** (com aviso), mas **continuam no arquivo**.
- `site` e `avaliacoes`: preenchidos pelo minerador (`minerador_maps.py`).
- `status`: `pendente`, `enviado` ou `nao_contatar` (opt-out).
- `resposta`: `aguardando`, `respondeu`, `sem resposta` ou `sem interesse`.
- `ultimo_envio` / `followups`: preenchidos automaticamente.
- CSVs antigos sem as colunas novas continuam funcionando, e colunas extras
  que você adicionar são preservadas.
- Sem CSV, apenas o `--preview` funciona (com exemplos fictícios); os demais
  modos param com erro, para nunca enviar a contatos de exemplo.

## Acompanhando os resultados

```bash
python prospeccao_whatsapp.py --respostas   # [s] respondeu  [n] sem resposta
                                             # [x] não contatar mais (opt-out)
python prospeccao_whatsapp.py --followup    # retomada para quem não respondeu
python prospeccao_whatsapp.py --listar      # vê a coluna RESPOSTA na tabela
```

## Modos de envio

- `--modo manual` (padrão): abre o WhatsApp Web no navegador com o texto
  pré-preenchido — você revisa e clica em **enviar**. Zero automação de envio.
  Se não houver navegador padrão, o link é exibido para cópia manual.
- `--modo web`: Selenium abre a conversa com o texto já na caixa, mas **para
  sem enviar**; só envia após você digitar `ENVIAR` no terminal. Um único
  navegador é reaproveitado durante a sessão, com perfil salvo
  (`.perfil_whatsapp_web/`) para manter o QR Code logado.
- `--preview`: gera prévias com variações novas sem tocar no navegador e sem
  restrição de horário.
- **Editar texto** (`[e]` na prévia): cole a mensagem (linhas vazias são
  mantidas) e termine com uma linha contendo apenas `.`.

## Modo automático (agendado)

```bash
python prospeccao_whatsapp.py --automatico --limite-dia 10 --atraso-max 40 --resumo-para 5579981677954
```

Envia **sem operador**, até a cota do dia (10, somando follow-ups e
apresentações):
1. **Follow-ups**: quem recebeu há **2 dias úteis** e não respondeu (ou só o
   robô respondeu) — no máximo 1 por clínica.
2. **Novos contatos** `pendente` — **celulares primeiro, depois fixos** (número
   sem WhatsApp é detectado sem enviar nada e vira `sem_whatsapp`).
3. Confere as entregas, lê as respostas, executa as ações automáticas (abaixo)
   e manda um **resumo para o seu WhatsApp** (`--resumo-para`).

Começa às **10h30** + um atraso sorteado de 0 a `--atraso-max` minutos (fora do
pico da recepção, que é logo cedo).

**Teste A/B** (coluna `variante`, fixa por número, metade para cada lado):
- `curta`: uma pergunta fácil para a recepção responder ("o WhatsApp de
  vocês costuma ficar muito cheio?") ou um pedido curto de permissão. Quando uma **pessoa** responde, o script envia sozinho a
  **apresentação** (`estruturas_apresentacao`) e a coluna `etapa` vira
  `apresentacao`;
- `completa`: a mensagem completa, terminando com a oferta de um resumo e o
  pedido de repassar para quem cuida da gestão (quem lê costuma ser a recepção).
  Quando uma **pessoa** responde, o script envia sozinho o **resumo**
  (`estruturas_resumo`) e a `etapa` vira `resumo`. Só vale para contatos
  abordados com textos versionados (`versao_textos` preenchida); quem recebeu
  os textos antigos continua sendo atendido à mão.
O resumo diário mostra quantas responderam em cada variante, contando só a
versão atual dos textos.

**Personalização**: com nota no Google ≥ 4,5, alguns textos elogiam a nota
("Vi as 4,9 estrelas da MB Odontologia no Google").

Nunca recebem mensagem automática:
- a **mesma clínica** por outro número (nome limpo igual → `mesma_clinica`);
- nomes da lista de bloqueio (`BLOQUEIO_NOMES`, inclui PROIMAGEM → `bloqueado`);
- nomes **genéricos** sem a marca ("Implantes Dentários", "Dentista 24Horas"):
  ficam pendentes e aparecem como "pulados" — corrija o nome no CSV.

### Acompanhamento ao longo do dia

```bash
python prospeccao_whatsapp.py --acompanhar --limite-dia 10 --resumo-para 5579981677954
```

Agendado como **"Prospeccao Acompanhamento"** às 12h, 14h, 16h e 18h (dias
úteis). Não prospecta ninguém novo; só:
- **avisa na hora** (seu WhatsApp + Windows) quando uma **pessoa** responde,
  com o trecho da resposta — para você assumir a conversa rápido;
- envia a 2ª etapa para quem respondeu (**apresentação** na curta, **resumo**
  na completa). **Não contam no limite diário** — senão o envio das 10h30
  esgotaria a cota e a resposta ficaria adiada; têm um teto próprio igual a
  `--limite-dia` por dia;
- em robôs com menu, escolhe a opção **"falar com atendente"** (uma vez por
  clínica; não conta no limite diário);
- marca `nao_contatar` quem pedir para não ser contatado (inclui "não é
  relevante", "não precisamos");
- resposta negativa sem ser opt-out ("Não", "Já temos", "No momento não"):
  você é avisado, mas o script **não** manda apresentação/resumo (`etapa` =
  `recusou`).

### Agendamento

| Tarefa | Quando |
|---|---|
| "Prospeccao WhatsApp" | Seg–sex, 10h30 (+0 a 40 min) |
| "Prospeccao Acompanhamento" | Seg–sex, 12h, 14h, 16h, 18h |
| "Prospeccao Minerador" | Segundas, 7h30 |

Se o PC estiver desligado no horário, o envio roda quando ligar (dentro da
janela 8h–19h). O PC precisa estar ligado e com o usuário logado; o login do
WhatsApp Web fica salvo em `.perfil_whatsapp_web/`.

Checagens em cada etapa (a meta é erro zero, então na dúvida ele **não envia**):

| Etapa | Checagem | Se falhar |
|---|---|---|
| Antes de começar | Dia útil (fim de semana e feriados nacionais, de SE e de Aracaju), janela de horário, cota do dia | Não faz nada |
| Antes de começar | Nenhum outro envio rodando (`.envio.lock`) | Não faz nada |
| Texto gerado | Sem tokens soltos, sem "PROIMAGEM"/"prévia", nome limpo presente, tamanho normal, sem parágrafo repetido | Pula o contato |
| Abrir conversa | Número sem WhatsApp | Marca `sem_whatsapp` e segue |
| Abrir conversa | WhatsApp Web desconectado (QR Code) | **Para tudo** |
| Caixa de texto | Contém **exatamente** o texto gerado (nada faltando, nada duplicado) | Limpa a caixa, tenta 1 vez de novo |
| Logo antes do ENTER | Número não recebeu nada hoje; texto ainda confere | Não envia |
| Depois do ENTER | Uma bolha nova com exatamente o texto aparece na conversa | Registra e **para tudo** |
| Geral | 2 falhas técnicas seguidas | **Para tudo** |
| Depois dos envios | Cada mensagem de hoje aparece **exatamente 1 vez** na conversa | Alerta no resumo e no Windows |

Avisos: o **resumo diário** chega no seu WhatsApp; se algo der errado (ou a
fila estiver acabando), aparece também uma **notificação do Windows** — útil
quando o próprio WhatsApp desconectou. Detalhes no log do dia em `logs/`.

### Respostas e verificação (somente leitura)

```bash
python prospeccao_whatsapp.py --verificar
```

Abre as conversas **sem digitar nada**, confere as entregas de hoje e
classifica as respostas: `respondeu` (pessoa), `resposta automatica` (robô:
resposta em até 2 min ou frases típicas — 4 de 5 clínicas do 1º lote têm robô)
ou `sem interesse` (vira `nao_contatar`). O modo automático faz isso todo dia.
Abrir a conversa marca as mensagens da clínica como lidas.

### Rodízio do minerador (semanal)

```bash
python minerador_maps.py --rodizio --max 40
```

Usa o próximo termo de `termos_mineracao.txt` (edite à vontade; sem clínicas
de imagem/diagnóstico) e inclui fixos. Só grava o CSV com a trava de envio,
para nunca colidir com o envio automático. Agendado como **"Prospeccao
Minerador"** (segundas, 7h30).

Acompanhe pelo log do dia em `logs/` e por `--listar`. Para pausar:
`Disable-ScheduledTask -TaskName "Prospeccao WhatsApp"` (PowerShell); para
voltar: `Enable-ScheduledTask -TaskName "Prospeccao WhatsApp"`.

Se o log disser **"WhatsApp Web desconectado"**, rode
`python prospeccao_whatsapp.py --conectar` e escaneie o QR Code no Chrome que
abrir (WhatsApp > Aparelhos conectados). Ele fecha sozinho quando conectar.
