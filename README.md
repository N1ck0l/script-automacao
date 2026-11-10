# Prospecção B2B Human-in-the-Loop — WhatsApp (Clínicas)

Script Python para prospecção **semi-supervisionada** de clínicas odontológicas e
médicas locais. Nenhum envio é 100% autônomo: **cada mensagem passa por revisão
e confirmação humana**, com limite diário, delay aleatório, janela de horário e
variação de texto (spintax) para reduzir o risco de banimento de chip pela Meta.

## Arquivos

| Arquivo | Descrição |
|---|---|
| `prospeccao_whatsapp.py` | Script principal de envio (somente stdlib; Selenium opcional) |
| `minerador_maps.py` | Minerador de empresas no Google Maps (Playwright) → `prospeccao.csv` |
| `prospeccao.csv` | Contatos (`clinica,bairro,cidade,telefone,site,avaliacoes,status,resposta,atualizado_em`) |
| `test_prospeccao.py` + `test_minerador.py` | 43 testes do núcleo anti-ban e da mineração (unittest, zero dependências) |
| `historico_envios.jsonl` | Auditoria: todo envio confirmado é registrado aqui |
| `requirements.txt` | `playwright`, `pandas`, `selenium` (opcional) |

## Como rodar

```bash
# 1. Instalação (uma vez)
pip install -r requirements.txt
playwright install chromium        # baixa o Chromium do Playwright

# 2. Minerar empresas do Google Maps -> prospeccao.csv (dedup por telefone)
python minerador_maps.py --termo "clinica odontologica em Aracaju SE" --max 40
python minerador_maps.py --termo "clinica medica em Aracaju SE" --max 30 --visivel
python minerador_maps.py --termo "dentista em Aracaju" --dry-run   # só visualizar

# 3. Fila de envio — prévia → sua confirmação → envio → delay 45–90s
python prospeccao_whatsapp.py

# Outros comandos úteis
python prospeccao_whatsapp.py --listar          # lista contatos + respostas
python prospeccao_whatsapp.py --preview         # dry-run: só gera prévias
python prospeccao_whatsapp.py --contato 3       # retoma a fila pelo 3º pendente
python prospeccao_whatsapp.py --respostas       # registra quem respondeu
python prospeccao_whatsapp.py --modo web        # automatiza o WhatsApp Web (Selenium)
python prospeccao_whatsapp.py --reset           # reabre a fila inteira
python prospeccao_whatsapp.py --limite-dia 15 --hora-inicio 9 --hora-fim 18

# 4. Testes (biblioteca padrão, sem instalar nada)
python -m unittest discover -v
```

Requisitos: Python 3.10+. O modo `manual` (padrão) não precisa de nada instalado.

## Minerador Google Maps (`minerador_maps.py`)

- **Entrada**: `--termo` de busca configurável e `--max` (padrão: 40 alvos).
- **Extração por card**: `clinica` (h1), `bairro` (parser de endereço com 4
  formatos cobertos), `cidade`, `telefone` (higienizado com DDI 55 — reutiliza
  `normalizar_telefone`/`validar_telefone` do script de envio), `site`
  (`data-item-id="authority"`), `avaliacoes` (nota + volume) e `status=pendente`.
- **Robustez/evasão básica**:
  - Seletores semânticos (`data-item-id`, `role`, `aria-label`, `href`) em vez
    de classes geradas pelo Google;
  - Rolagem suave incremental no `div[role="feed"]` lateral até o limite;
  - Pausas aleatórias de 1–3s (`--delay-min`/`--delay-max`) entre navegações;
  - Fallback campo a campo: um elemento que falha não derruba a mineração.
- **Saída**: integra direto no `prospeccao.csv` do script de envio —
  **nunca sobrescreve** registros existentes; duplicidade verificada por
  **telefone** (higienizado). Usa `pandas` para a integração (com fallback
  para a biblioteca padrão, se o pandas não estiver instalado).
- **Aviso**: a mineração consulta dados públicos em baixo volume; respeite os
  Termos de Serviço do Google e mantenha as pausas — volume alto gera CAPTCHA
  e bloqueio. O script para automaticamente se a lista lateral não aparecer.

## Fluxo de execução (sempre semi-supervisionado)

1. Itera **apenas** sobre contatos com `status == 'pendente'`.
2. **Valida** telefone (55 + DDD + número), **descarta duplicatas** e bloqueia
   reenvio para o mesmo número no mesmo dia.
3. Monta o texto dinâmico (spintax + personalização por clínica/bairro/cidade).
4. Codifica com `urllib.parse.quote()` e gera:
   `https://web.whatsapp.com/send?phone={telefone}&text={mensagem}`
5. Abre o link no navegador padrão via `webbrowser` (prévia exibida **uma única
   vez**, antes da sua aprovação).
6. Imprime o alvo no terminal e **pausa**:
   `Pressione ENTER após revisar e enviar a mensagem no WhatsApp Web...`
7. Atualiza `status` para `enviado` + timestamp no CSV (salvamento atômico).
8. Aplica delay aleatório obrigatório de **45 a 90 segundos**
   (`time.sleep(random.randint(45, 90))`) com contagem regressiva amigável.

## Proteções anti-ban

- **Nunca envia sozinho**: sem sua ação explícita (`s` / `ENTER` / digitar `ENVIAR`), nada sai.
- **Janela de horário**: 08h–19h por padrão (`--hora-inicio` / `--hora-fim`) — fora dela a fila é bloqueada.
- **Limite diário**: padrão 20 mensagens/dia (`--limite-dia`).
- **Sem reenvio no mesmo dia**: consulta o histórico por número; se já recebeu hoje, exige confirmação explícita para forçar.
- **Cooldown humano**: 45–90s entre envios, com contagem regressiva.
- **Deduplicação + validação de telefone** no carregamento do CSV.
- **Spintax**: saudação, gancho, autoridade, case e CTA variam a cada geração — nenhuma mensagem idêntica.
- **Auditoria**: cada envio confirmado vira uma linha em `historico_envios.jsonl`.
- **Ctrl+C seguro**: `KeyboardInterrupt` salva o progresso antes de encerrar.

## Preparando os dados

Edite `prospeccao.csv` mantendo o cabeçalho:

```csv
clinica,bairro,cidade,telefone,site,avaliacoes,status,resposta,atualizado_em
Ferraz Odontologia,São José,Aracaju,5579999631871,,,pendente,,
```

- `telefone`: número internacional sem `+` (ex.: `5579999631871`). Números locais
  recebem o `55` automaticamente; inválidos e duplicados são ignorados com aviso.
- `site` e `avaliacoes`: preenchidos pelo minerador (`minerador_maps.py`).
- `status`: `pendente` ou `enviado`.
- `resposta`: preenchido pelo comando `--respostas` (`respondeu` / `sem resposta`).
- CSVs antigos sem as colunas novas continuam funcionando.
- Se o arquivo não existir, o script usa uma lista mockada de exemplo.

## Acompanhando os resultados

```bash
python prospeccao_whatsapp.py --respostas   # percorre os "enviados" e registra
                                             # quem respondeu (fecha o ciclo)
python prospeccao_whatsapp.py --listar       # vê a coluna RESPOSTA na tabela
```

## Modos de envio

- `--modo manual` (padrão): abre o WhatsApp Web no navegador com o texto
  pré-preenchido — você revisa e clica em **enviar**. Zero automação de envio.
  Se não houver navegador padrão, o link é exibido para cópia manual.
- `--modo web`: Selenium digita a mensagem na conversa, mas **para sem enviar**;
  só envia após você digitar `ENVIAR` no terminal. Usa perfil salvo
  (`.perfil_whatsapp_web/`) para manter o QR Code logado.
- `--preview`: gera prévias com variações novas sem tocar no navegador e sem
  restrição de horário.
