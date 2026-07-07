# Docker bring-up

## Prereqs
Docker + Docker Compose. ~4GB RAM free for Batfish (more for large networks; set
`--memory` if needed).

## Start
```
cd docker
docker compose up -d
docker compose ps          # both containers should be "running"
docker compose logs -f batfish-mcp
```

## Verify the engine
Batfish listens on 9996/9997. The MCP container reaches it by service name
`batfish` on the shared `netguard` network.

## Verify the MCP server + list tools
The MCP server is exposed on `http://localhost:8080` (confirm the exact endpoint
path — often `/mcp`). List tools with an MCP client or the inspector:
```
npx @modelcontextprotocol/inspector
# connect to the HTTP endpoint, then call tools/list
```
Record the EXACT tool names — the middleware may rename/hide vs the source
package. Update the app's tool-name map accordingly.

## Gotchas
- **Pin versions.** Replace `:latest` on the MCP image with a specific tag; this
  is a young project and `latest` can shift under you.
- **Env var names.** The repo's compose file is the source of truth for the MCP
  container's env vars (BATFISH_HOST, TRANSPORT, PORT). Cross-check against
  `docker-compose.yml` in the upstream repo; adjust here if they differ.
- **Transport.** If your client can only do stdio (not HTTP), you'll need a
  gateway or a stdio-mode run. Confirm what your host/Commotion supports.
- **Memory.** Batfish can OOM on big snapshots. Add
  `deploy.resources.limits.memory` and consider `--oom-kill-disable` semantics.
- **Snapshot lifetime.** Snapshots live in the engine's memory/volume for the
  container's lifetime. Restarting `batfish` clears them — re-init after restart.
```
