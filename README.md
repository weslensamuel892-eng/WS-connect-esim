# WS Connect eSIM Bot - Guia de Configuração (Railway)

Este bot foi atualizado para integrar com a gateway **Ironpay** e está pronto para ser implantado na **Railway**.

## 🚀 Como subir na Railway

1.  **Crie um novo projeto** na Railway e conecte seu repositório GitHub (ou use a CLI da Railway).
2.  **Variáveis de Ambiente:** No painel da Railway, vá em **Variables** e adicione as seguintes chaves:
    *   `TELEGRAM_BOT_TOKEN`: O token que você recebeu do @BotFather.
    *   `IRONPAY_TOKEN`: Seu token da Ironpay (`sz1Rt9JITY5MuWVNnraYwOgQ3CX4vtw76u4gp4M1Y8zCqNu3AVJTJO9onjMd`).
    *   `IRONPAY_OFFER_HASH`: O hash da oferta que você criou (`eijjfftylw`).
    *   `ADMIN_IDS`: Seu ID do Telegram (`7748272760`).
3.  **Arquivos incluídos:**
    *   `bot.py`: O código principal do bot.
    *   `requirements.txt`: Lista de dependências para instalação automática.
    *   `Procfile`: Comando para a Railway rodar o bot.

## 📁 Estrutura de Pastas
O bot cria automaticamente as pastas `stock` e `sold`.
*   Para adicionar estoque, você deve colocar os arquivos PDF na pasta `stock/<OPERADORA>/<PLANO>/`.
    *   Exemplo: `stock/Claro/5GB/esim1.pdf`

## ⚠️ Observação sobre o Banco de Dados
Por padrão, o bot usa SQLite (`payments.db`). Na Railway, se você não configurar um **Volume Persistente**, os dados do banco e o estoque serão apagados toda vez que o bot for reiniciado. 
**Recomendação:** Adicione um "Mount" de volume na pasta `/home/ubuntu/ws_connect_bot` se possível, ou utilize um banco de dados externo.

---
Desenvolvido por Manus para WS Connect eSIM.
*Última atualização: 17 de Junho de 2026 - Deploy Trigger*
