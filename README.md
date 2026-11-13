# redondo.ha

Home Assistant side of the Redondo Companion: the notification server the round touch display connects to.

Status: empty, nothing implemented yet.

## Protocol

The Companion firmware opens a TCP connection to this server (address fixed at build time with `COMPANION_SERVER := <ipv4>:<port>` in the firmware repo's `local.mk`). The server sends one line per notification:

```
kind|urgent|seconds|title|body
email|0|15|3|
urgent-email|1|60|Banco do Brasil|Confirme seu cadastro até sexta-feira
deal|1|60|SSD NVMe 2 TB|R$ 599 na Kabum, 32% abaixo do preço pedido
```

- `kind` picks the screen (`email`, `urgent-email`, `deal`; any other kind shows as text only).
- `urgent` is `0` or `1` and picks the queue.
- `seconds` (positive) is the most the notification stays on screen.
- Text is shown in Latin-1 only.

The full rules live in the firmware repo, `ai-context/decisions/companion-eyes-notifications.md`; its `tools/notify.py` is a stand-in for this server.
