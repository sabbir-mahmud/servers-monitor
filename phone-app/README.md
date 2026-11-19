# NexGuard phone app

The Android and iOS app that connects a phone to a NexGuard dashboard. Built with Expo
SDK 58 (React Native 0.88). Release builds are made on EAS: project
[`@sabbirmahmud/nexguard`](https://expo.dev/accounts/sabbirmahmud/projects/nexguard).

It scans the QR code on the dashboard's pair page (or takes the address and code typed
in). It then shows exactly what the account will see, and pairs only on **Allow**.
Afterwards it reports to `POST /device/v1/checkin`: battery, free and total storage,
network (Wi-Fi, mobile data or offline), model and system version, and location if the
person allowed it. It reports while open, each time it's opened, and from the background
when the system allows (at most every 15 minutes). The token is kept in the Keychain /
Keystore (`expo-secure-store`).

## Android: build it, and the dashboard hands it out

```sh
make phone-app-apk      # or: cd phone-app && npm run build:android
```

This builds the `production` profile on EAS: a signed `.apk`, with the version number
counted up on each build. It waits for the build, then downloads it into `dist/`. The
dashboard serves the newest `.apk` there at `/d/app/NexGuard.apk` (`PHONE_APP_DIR`). The
Android pair page shows a QR code: scanned with the phone's camera, it downloads the app.
Android asks once to allow installing from the browser.

- The first build asks to create the Android signing key; EAS keeps it. Every later build
  must use the same key, or phones can't update over the app they have.
- `./scripts/android-apk.sh --latest` downloads the newest finished build without
  building again (say, on the server that runs the dashboard).
- An app installed from a download doesn't update by itself: install the new `.apk` from
  the same link and it replaces the old one, keeping the pairing.

## iPhone: TestFlight or the App Store

iPhones can't install an app from a download link. Build and send it to App Store
Connect (this needs an Apple Developer account, $99 a year):

```sh
cd phone-app && npm run build:ios      # eas build --platform ios --profile production --auto-submit
```

Then in App Store Connect, either:
- open **TestFlight**, add an external group and turn on its **public link** (Apple
  reviews the first build, usually within a day); or
- release it on the App Store.

Set the dashboard's `IOS_APP_URL` to that link. The iPhone pair page then shows it with a
QR code.

For a few of your own iPhones without TestFlight, the `preview` profile makes an ad hoc
build. EAS asks you to register each iPhone first (`eas device:create`).

## Develop in Expo Go

1. Make the dashboard reachable from the phone (same network, listening on its address):

   ```sh
   # main/.env
   ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.20
   DASHBOARD_URL=http://192.168.1.20:8000
   ```

   ```sh
   cd main && ../venv/bin/python manage.py runserver 0.0.0.0:8000
   ```

   Use your computer's address in place of `192.168.1.20`. The pairing QR code holds
   `DASHBOARD_URL`. A code made while that was `127.0.0.1` won't work on a phone: the pair
   page warns about it.
2. `make phone-app` (or `npx expo start`), then open it in Expo Go.

Expo Go runs nothing in the background, so there it reports only while open. The EAS
builds don't have that limit. The status screen says which one is running.

**`http://` dashboards.** The release builds connect to `http://` as well as `https://`,
because a self-hosted dashboard often runs on plain `http://` on a home network. This
turns on cleartext traffic on Android (`expo-build-properties`) and sets
`NSAllowsArbitraryLoads` on iOS. Over `http://` the device token and the readings cross
the network unencrypted, so use `https://` for a dashboard reachable from the internet.
App Store review may ask why `NSAllowsArbitraryLoads` is on. The answer is that it
connects to self-hosted servers whose address the person types in.

## Layout

| File | What it does |
|---|---|
| `index.ts` | Defines the background task (it must exist at load), then starts the app |
| `App.tsx` | Pairing screen when not paired, status screen when paired |
| `src/PairScreen.tsx` | QR scan or typed code, then the disclosure, then pair on *Allow* |
| `src/StatusScreen.tsx` | Last report, readings, *Report now*, *Share location*, *Unpair* |
| `src/checkin.ts` | One check-in (shared by the timer and the background task). A 401 forgets the pairing |
| `src/report.ts` | Battery, storage, network, location, each best effort |
| `src/api.ts` | `/device/v1/pair` (preview and real) and `/device/v1/checkin` |
| `src/pairLink.ts` | Parses `nexguard://pair?server=…&code=…` and checks codes and addresses |
| `src/session.ts` | The pairing in `expo-secure-store` |

## Check

```sh
npm run typecheck && npm run lint && npx expo-doctor
```
