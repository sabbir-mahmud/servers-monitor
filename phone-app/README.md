# NexGuard phone app

The Android and iOS app that connects a phone to a NexGuard dashboard. Built with Expo
SDK 58 (React Native 0.88). Release builds are made on EAS: project
[`@sabbirmahmud/nexguard`](https://expo.dev/accounts/sabbirmahmud/projects/nexguard).

The person types the 6-digit code from the dashboard's pair page. The app shows who will
see the phone and what they can turn on (a preview, which doesn't use the code), asks for
each permission it needs with the reason, and only then pairs. It reports battery,
storage, network, model, system version and location; on Android 1.2 also screen time,
websites visited and blocking, and it can be beeped and protected from removal (see
[docs/devices.md](../docs/devices.md#android-controls-nexguard-12)).

**It never talks to the dashboard.** Everything goes through the Redis relay over
Upstash's REST API: pairing with the built-in login (`.env`, made with
`manage.py phone_app_login`), then with the phone's own login, allowed to `RPUSH` to
`sm:d:<id>:in` and `GET` its `:cfg` and `:cmd`. Check-ins are signed (HMAC-SHA256,
`@noble/hashes`) with a key the phone made; the dashboard signs settings and commands with
the same key, and nothing unsigned is obeyed. The login and key are kept in the Keychain /
Keystore.

**Android controls** are a local Expo module, `modules/nexguard-guard` (Kotlin): a
foreground service that is the website filter (a VPN routing only its DNS address),
checks for a beep every 30 s and notices protections being turned off; screen time from
`UsageStatsManager`; removal protection as a device admin. It needs a real build: in
Expo Go and on iPhones the module isn't there and the app does what 1.1 did.

**Test the Android controls** with an installable build (no Android SDK needed locally):

```sh
cd phone-app && npx -y eas-cli@latest build --platform android --profile preview
```

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

`make phone-app` (or `npx expo start`), then open it in Expo Go. The phone only needs the
internet: it talks to Upstash, never to the dashboard, so the dashboard can stay on
`127.0.0.1`. Run the relay process (`make relay`) so pairing gets an answer.

Expo Go has no Android controls (it can't load the native module) and runs nothing in the
background: it reports while open, and tells the dashboard it has no controls
(`nexguard-phone 1.2.0-nocontrols`), so none are offered for it.

## Layout

| File | What it does |
|---|---|
| `index.ts` | Defines the background task (it must exist at load), then starts the app |
| `App.tsx` | Pairing screen when not paired, status screen when paired |
| `src/PairScreen.tsx` | The 6-digit code, the preview, then pairing |
| `src/SetupScreen.tsx` | Who will see the phone, then each permission with its reason |
| `src/StatusScreen.tsx` | Last report, readings, SOS, what's missing; *Unpair* only on a phone without controls |
| `src/checkin.ts` | One check-in (shared by the timer and the background task). A 401 forgets the pairing |
| `src/relay.ts` | Preview, pairing, signed check-ins and SOS, reading the signed settings |
| `modules/nexguard-guard` | Android: filter service, screen time, beep, removal protection (Kotlin) |
| `src/report.ts` | Battery, storage, network, location, each best effort |
| `src/api.ts` | Shared types and the error the screens show (the app never calls the dashboard) |
| `src/session.ts` | The pairing in `expo-secure-store` |

## Check

```sh
npm run typecheck && npm run lint && npx expo-doctor
```
