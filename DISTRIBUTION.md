# stt Distribution Plan

This project is currently optimized for local macOS development and manual customer/demo validation. Distribution steps are intentionally documented and opt-in; this repo does not run Developer ID signing, notarization, stapling, or Homebrew publishing during normal validation.

## Local development path

Use the existing app-bundle build script:

```bash
./scripts/build-app-bundle.sh
./scripts/check-app-bundle.sh
./dist/stt.app/Contents/MacOS/stt doctor
```

By default, `build-app-bundle.sh` signs `dist/stt.app` ad-hoc:

```text
CODESIGN_IDENTITY=-
HARDENED_RUNTIME=0
```

This keeps local builds fast, Apple-ID-free, and suitable for TCC attribution testing. The bundle identifier remains overrideable for local experiments:

```bash
BUNDLE_ID=com.example.stt ./scripts/build-app-bundle.sh
```

## Developer ID signing

Developer ID signing is a maintainer-only release step. Prerequisites:

1. Apple Developer Program membership.
2. A valid `Developer ID Application` certificate installed in the login keychain.
3. Access to the team-specific app bundle ID intended for release.

Check local signing identities:

```bash
security find-identity -v -p codesigning
```

Release signing should be run explicitly by a maintainer:

```bash
CODESIGN_IDENTITY="Developer ID Application: Example, Inc. (TEAMID)" \
HARDENED_RUNTIME=1 \
CONFIGURATION=release \
./scripts/build-app-bundle.sh
```

Do not use a personal or customer Team ID for shared release artifacts. For local development, keep the default ad-hoc signing path.

## Hardened runtime

`HARDENED_RUNTIME=1` adds `--options runtime` to `codesign`. Use it only with a real Developer ID signing identity.

The current entitlement file is intentionally narrow:

```text
Resources/stt-app/stt.entitlements
```

It includes microphone/audio input access for recording workflows. Do not add broad hardened-runtime exceptions such as JIT, unsigned executable memory, library validation disablement, or Apple Events unless a signed release build proves they are required.

## Notarization plan

Notarization is not run by validation scripts. Once a Developer-ID-signed release bundle exists, package and notarize it manually:

```bash
ditto -c -k --keepParent dist/stt.app dist/stt.app.zip
xcrun notarytool submit dist/stt.app.zip --keychain-profile "stt-notary" --wait
xcrun stapler staple dist/stt.app
xcrun stapler validate dist/stt.app
spctl --assess --type execute --verbose=4 dist/stt.app
```

The `stt-notary` keychain profile should be created out of band by the maintainer using Apple-approved credentials. Do not commit Apple IDs, app-specific passwords, API keys, issuer IDs, or private keys.

## Homebrew packaging plan

Because `stt` is distributed as a `.app` for correct TCC attribution, the preferred Homebrew shape is a cask that installs a signed and notarized release archive. A scaffold lives at:

```text
packaging/homebrew/stt.rb
```

Before publishing, replace the placeholder URL and SHA-256 with values from an actual signed/notarized release archive:

```bash
shasum -a 256 stt-macos-universal.zip
```

Publishing checklist:

1. Build `CONFIGURATION=release` with a Developer ID identity and hardened runtime.
2. Zip, notarize, staple, and validate `stt.app`.
3. Upload the immutable archive to the chosen release location.
4. Update the Homebrew cask URL, version, and SHA-256.
5. Test install from a local tap before publishing.

## Release validation checklist

Run these before announcing a release:

```bash
./scripts/check-app-bundle.sh dist/stt.app
./dist/stt.app/Contents/MacOS/stt doctor
./scripts/manual-native-tap-smoke.sh
```

Optional/manual gates remain manual:

```bash
STT_SYSTEM_DEVICE="BlackHole 2ch" ./scripts/validate.sh
STT_RESET_TCC=1 ./scripts/manual-tcc-smoke.sh
./scripts/bootstrap-python-backend.sh --mlx --check
```
