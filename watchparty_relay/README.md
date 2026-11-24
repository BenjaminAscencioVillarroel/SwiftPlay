# SwiftPlay WatchParty Relay (Cloudflare Worker + Durable Object)

Relay WebSocket ultraliviano (plan gratuito de Cloudflare) para presencias en tiempo real (`Activo` / `Inactivo` / `Desconectado`) y control cruzado de WatchParty entre usuarios de SwiftPlay.

## Despliegue rápido (1 sola vez, ~2 minutos)

```powershell
cd watchparty_relay
npx wrangler login
npx wrangler deploy
```

Al terminar, `wrangler deploy` te dará una URL parecida a:
`https://swiftplay-watchparty.<tu-usuario>.workers.dev`

Copia esa URL y pégala en **SwiftPlay → WatchParty → Configurar servidor** (o con `wss://swiftplay-watchparty.<tu-usuario>.workers.dev/ws`) y compártela con tus socios.
