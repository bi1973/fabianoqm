# Mendeley Controlled Read-Write Bridge

Backend para um GPT personalizado consultar e modificar uma biblioteca pessoal do Mendeley com controles de segurança.

## Proteções implementadas

- toda criação, atualização, importação de PDF e criação de coleção exige uma **prévia**;
- a gravação usa uma segunda rota marcada como consequencial e exige `CONFIRMO SALVAR`;
- referências são verificadas por DOI e por título/ano antes da criação;
- PDFs são verificados por SHA-1 e limitados a 10 MB;
- arquivos enviados pelo ChatGPT são aceitos apenas por links HTTPS temporários do domínio `oaiusercontent.com`;
- o PDF é guardado temporariamente em `/tmp`, com expiração de quatro minutos;
- não existem rotas de exclusão no backend nem no esquema OpenAPI;
- o token de prévia é assinado e vinculado à sessão OAuth do usuário;
- eventos são registrados nos logs e em `/tmp/mendeley-bridge-audit.jsonl`;
- há limitação básica de solicitações por usuário.

## Funções

### Leitura

- perfil conectado;
- coleções;
- referências;
- metadados completos;
- arquivos e PDFs anexados;
- eventos recentes de auditoria.

### Escrita controlada

- criar referência por metadados;
- corrigir metadados de uma referência existente;
- criar coleção;
- importar PDF como nova referência, usando a extração de metadados do Mendeley;
- anexar PDF a uma referência existente;
- adicionar uma nova referência a uma coleção.

## Publicação no Render

1. Conecte este repositório ao Render.
2. Escolha **Blueprint** e selecione `render.yaml`, ou crie um Web Service manualmente.
3. O Render criará `SIGNING_SECRET` automaticamente.
4. Aguarde o endereço público ficar ativo.
5. Caso o endereço não seja `https://mendeley-controlled-writer.onrender.com`, substitua esse domínio em `openapi-controlled.json`.

O `render.yaml` usa um único processo Gunicorn porque as prévias de PDF são temporárias e locais à instância.

## Configuração OAuth da Action

No editor do GPT, configure OAuth usando o mesmo domínio do backend:

- URL de autorização: `https://SEU-DOMINIO/oauth/authorize`
- Token URL: `https://SEU-DOMINIO/oauth/token`
- Escopo: `all`
- Método de troca: cabeçalho de autorização básica

O bridge encaminha o fluxo para:

- `https://api.mendeley.com/oauth/authorize`
- `https://api.mendeley.com/oauth/token`

Cadastre no aplicativo do Mendeley a URL de callback exibida pelo editor do GPT, exatamente como apresentada.

## Instalação local

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export SIGNING_SECRET="um-segredo-longo-e-aleatorio"
flask --app app run --port 10000
```

No Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SIGNING_SECRET="um-segredo-longo-e-aleatorio"
flask --app app run --port 10000
```

## Testes básicos

Sem OAuth, apenas saúde e status ficam disponíveis:

```bash
curl http://localhost:10000/
curl http://localhost:10000/health
```

As rotas `/api/*` exigem `Authorization: Bearer <token-do-Mendeley>`.

## Limitações conhecidas

- o arquivo de auditoria e o cache de PDFs ficam no armazenamento temporário do Render; os logs do Render são a principal trilha operacional;
- uma reinicialização do serviço invalida prévias de PDF ainda não confirmadas;
- se a referência for criada e a inclusão na coleção falhar, o bridge não apaga a referência criada. Ele devolve `folder_assignment.status = failed` para correção posterior;
- não há automação de pastas locais do Windows neste backend. Isso requer um agente local separado.

## Política de segurança

Nunca publique Client Secret, access token, refresh token ou arquivos `.env`. O backend não armazena credenciais OAuth do Mendeley. O Client ID e o Secret permanecem no editor do GPT, e o proxy OAuth apenas encaminha a troca para o Mendeley.
