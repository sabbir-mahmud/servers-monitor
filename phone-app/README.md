# NexGuard phone app

The Android and iOS app that connects a phone to a NexGuard dashboard. Built with Expo
SDK 58 (React Native 0.88). It runs in **Expo Go**, with nothing to build.

It scans the QR code on the dashboard's pair page (or takes the address and code typed
in). It then shows exactly what the account will see, and pairs only on **Allow**.
Afterwards it reports to `POST /device/v1/checkin`: battery, free and total storage,
network (Wi-Fi, mobile data or offline), model and system version, and location if the
person allowed it. The token is kept in the Keychain / Keystore (`expo-secure-store`).

## Run it in Expo Go

1. Make the dashboard reachable from the phone. Both must be on the same network, and
   the dashboard must listen on its network address:

   ```sh
   # main/.env
   ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.20
   DASHBOARD_URL=http://192.168.1.20:8000
   ```

   ```sh
   cd main && ../venv/bin/python manage.py runserver 0.0.0.0:8000
   ```

   Use your computer's address in place of `192.168.1.20`. The QR code holds
   `DASHBOARD_URL`. A code made while that was `127.0.0.1` won't work on a phone: the pair
   page warns about it, so make a new one.
2. Install **Expo Go** on the phone (Google Play or the App Store).
3. Start the app: `make phone-app` (or `cd phone-app && npx expo start`). Scan the QR code
   that Metro prints: with the Camera app on iOS, or from Expo Go on Android.
4. On the dashboard: **Devices → Add device → Android / iPhone**. In NexGuard, choose
   **Scan the QR code** and point it at the pair page.

## What Expo Go can't do

Expo Go runs nothing in the background, on Android or iOS. So in Expo Go the phone
reports when NexGuard opens, every 15 minutes while it's open, and each time it comes
back to the front. The dashboard shows it offline once it's been away for more than about
34 minutes.

A development or store build (`npx expo run:android`, `npx expo run:ios`, or
`eas build`) needs no code changes. It also checks in from the background
(`expo-background-task`, at most every 15 minutes, when the system allows). The status
screen says which one it's running.

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
