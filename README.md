# Mendeley Controlled Read-Write Bridge

Este repositório será usado para substituir o bridge atual, que expõe apenas operações de leitura, por uma versão com escrita controlada.

## Política de segurança

- leitura livre da biblioteca autorizada;
- criação somente em duas etapas: prévia e confirmação;
- confirmação explícita antes de gravar;
- verificação de duplicidade por DOI e título;
- nenhuma rota de exclusão;
- nenhuma alteração silenciosa de referências existentes;
- upload limitado a PDF autorizado;
- registro de auditoria das gravações.

## Situação atual

O GPT usa hoje o servidor:

`https://mendeley-chatgpt-bridge.fabianoqm.chatgpt.site`

O esquema atual contém apenas operações `GET`. Alterar somente o esquema OpenAPI não cria funções de escrita. Primeiro é necessário publicar um backend que implemente os novos endpoints; depois o endereço em `servers.url` e o esquema do GPT serão atualizados.
