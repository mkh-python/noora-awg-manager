# Noora AWG Manager

Noora AWG Manager is an automated installer and Telegram management bot for AmneziaWG servers.

It is designed for server owners who want to install, manage, monitor, and back up AmneziaWG users through a simple Telegram bot interface.

## Installation

Run this command on a fresh Ubuntu server:

    sudo bash -c "$(wget -qO- https://raw.githubusercontent.com/mkh-python/noora-awg-manager/main/install.sh)"

The installer will ask for:

- Telegram Bot Token
- Telegram numeric Owner ID

After installation, open your Telegram bot and send:

    /start

## Main Features

- Automatic Telegram bot installation
- AmneziaWG user creation
- QR code generation
- Config file delivery
- Install links for Android, iPhone, and Windows
- Traffic quota management
- Expiration date management
- Enable and disable users
- Delete users
- Extend user traffic and expiry
- Admin management
- Backup channel setup
- Full server backup
- Automatic scheduled backups
- Server status report
- Domain and SSL management
- Web panel URL management

## Telegram Bot Features

The Telegram bot allows admins to manage users without logging into the server.

Available actions include:

- Create new VPN users
- Send config file and QR code
- Show user status
- Check traffic usage
- Extend user duration
- Extend traffic limit
- Disable users
- Enable users
- Delete users
- Manage bot admins
- Configure backup channel
- Configure backup frequency
- Configure domain and SSL
- View server health

## Client App Links

When a new user is created, the bot sends:

- Config file
- QR code
- Android install link
- iPhone install link
- Windows install link
- Short setup guide

This helps end users connect without needing technical knowledge.

## Backup System

Noora AWG Manager can create full backups of important server data, including:

- AmneziaWG configuration
- User configs
- Bot database
- Bot configuration
- Web panel database
- Nginx configuration
- SSL certificates
- Firewall rules
- Restore information

Backups can be sent automatically to a Telegram channel.

## Recommended Server

Minimum:

- Ubuntu 22.04 or 24.04
- 1 vCPU
- 1 GB RAM
- Public IPv4 address

Recommended:

- Ubuntu 24.04
- 2 vCPU
- 2 GB RAM
- Public IPv4 address
- Clean server

## Important Notes

Use this installer on a fresh server.

Do not run the installer on a production server that is already configured unless you know exactly what you are doing.

The installer may overwrite bot files, service files, and related configuration.

## Project Structure

    noora-awg-manager/
    ├── install.sh
    ├── update.sh
    ├── uninstall.sh
    ├── restore.sh
    ├── bot/
    │   ├── bot.py
    │   ├── requirements.txt
    │   └── config.env.example
    ├── scripts/
    │   ├── backup.sh
    │   ├── send-backup.sh
    │   └── restore.sh
    ├── systemd/
    │   ├── awg-bot.service
    │   ├── awg-full-backup.service
    │   └── awg-full-backup.timer
    └── README.md

## Roadmap

Planned improvements:

- Full automatic AmneziaWG server installer
- Full automatic AmneziaWG web panel installer
- One-command restore system
- Web dashboard improvements
- Multi-server support
- Better usage analytics
- Multi-language bot messages

## Persian Description

مدیریت کامل سرور AmneziaWG از طریق ربات تلگرام.

این پروژه برای کسانی ساخته شده که می‌خواهند بدون نیاز به ورود مستقیم به سرور، کاربران VPN را بسازند، حذف کنند، تمدید کنند، حجم بدهند، تاریخ انقضا بگذارند و بکاپ کامل بگیرند.

بعد از نصب، بیشتر کارها از داخل ربات تلگرام انجام می‌شود.

## License

This project is provided as-is.

Use it at your own risk.
