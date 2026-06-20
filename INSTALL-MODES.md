# Installation modes

Run as root:

```bash
bash install.sh
```

## 1. Full installation
Installs AmneziaWG when missing, creates the default `awg0` server config, then installs Noora Bot.

## 2. Bot-only installation
For servers where AmneziaWG is already installed. The installer:

- detects the existing AWG configuration;
- reads its interface name and listen port;
- does not overwrite the AWG config;
- does not generate new server keys;
- does not remove peers;
- does not restart the existing AWG interface during installation;
- installs only Noora Bot, its Python environment, services and backup scripts.

The installer asks for only the Telegram bot token and owner ID when automatic AWG detection succeeds.
