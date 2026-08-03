# Codex Terminal

Terminal interface for OpenAI's Codex CLI (`@openai/codex`), running as a Home
Assistant add-on. Mirrors the structure of the `claude-terminal` add-on in
this repo: Alpine base image, npm-installed CLI, `ttyd` web terminal exposed
via ingress, and persistent auth under `/data` so `codex login` survives
add-on restarts and updates (`CODEX_HOME=/data/.codex`).

## Login

`codex login` needs a browser to complete OAuth. From the add-on's web
terminal, run `codex login`, copy the URL it prints, and open it on a
machine with a browser. If the flow redirects to a `localhost` callback
port, forward that port from the add-on host to your browser machine, e.g.:

```
ssh -L 1455:localhost:1455 root@<ha-host>
```

then open the printed URL in a browser on the machine running that `ssh`
command.
