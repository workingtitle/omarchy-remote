# Omarchy Remote

Use your Omarchy desktop from an iPhone, iPad or Mac. This repository holds the part that runs on the
machine: the installer, the connection service, screen capture and input. The apps for iPhone, iPad and
Mac are distributed separately.

## What you need

- **The machine:** Omarchy, or another Arch system running Hyprland.
- **The network:** Tailscale on the machine and on every device, signed in to the same account.
- **The apps:** Omarchy Remote for iPhone, iPad or Mac.

## Install

On the machine, in a terminal:

```sh
git clone https://github.com/workingtitle/omarchy-remote
omarchy-remote/native/install.sh
```

The installer adds any missing packages and asks for your password once. It sets up the connection
service, publishes it to your tailnet (never to the internet) and puts an Omarchy Remote icon in the bar.

## Pair a device

Click the Omarchy Remote icon in the bar, or run `omarchy-remote-pair`. Scan the QR code with your iPhone
or iPad, or enter the address and the six-digit code in the Mac app. Then approve the device on the
machine. The app's guide (in the app) explains the rest.

## Update

```sh
cd omarchy-remote && git pull && native/install.sh
```

## Check or remove

- `native/install.sh --check` lists every part as OK or FIX, with the command that fixes it. It changes nothing.
- `native/install.sh --uninstall` removes the installed files. Packages stay installed.
