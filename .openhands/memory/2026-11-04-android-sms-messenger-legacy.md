# Project memory — /workspace/project

Android SMS messenger app, Kotlin + Jetpack Compose + Room, package
`com.sms.messages`, minSdk 26 / targetSdk 35. Arabic (`values-ar`) and English.

## Build environment
- `ANDROID_HOME=/opt/android-sdk`, `JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64`,
  `GRADLE_USER_HOME=$HOME/.gradle`. Gradle 8.9 via wrapper.
- **Never pass `--no-daemon`.** `gradle.properties` enables daemon, parallel, build
  cache, configuration cache and VFS watching. Warm incremental debug build ≈ 2s.
- `bash tools/gbuild.sh <gradle tasks>` — daemon build, waits on the log via
  `inotifywait` (no sleeps), prints only errors/warnings/failures. Full check in one call:
  `:app:testDebugUnitTest :app:lintDebug` (≈1 min).
- `bash tools/audit.sh` — one-pass static audit (imports, string usage both directions,
  hardcoded UI text, icon/drawable existence). Batch edits, then build once.
- Read results from the XML, not the log: `app/build/test-results/testDebugUnitTest/*.xml`
  for failures, `app/build/reports/lint-results-debug.xml` for lint (incl. `UnusedResources`,
  which never reaches the console).

## Architecture
- `SmsRepository` is the single entry point for conversation bookkeeping; receivers,
  schedulers and screens all go through it. `SmsApp` owns the per-process singletons.
- `PhoneNumbers.normalize` collapses local / `+966` / `00` forms to E.164 so one contact
  maps to one conversation. Keep pure-logic utils free of Android resource lookups so plain
  JVM tests can cover them; pass localised words in as parameters.
- All user-visible text goes through `R.string`; `LocalizationTest` enforces English/Arabic
  parity and matching placeholders.
- DB bootstrap: `EnsureCreated()` for a fresh install, plus **real Room migrations** for
  existing installs (`data/Migrations.kt`). `app/schemas/` is committed. **Never use
  `fallbackToDestructiveMigration`** — it would wipe users' messages.
- DI is Hilt 2.52 (`@HiltAndroidApp` SmsApp, `di/AppModule`, `@HiltViewModel` +
  `hiltViewModel()`). Receivers/services are `@AndroidEntryPoint` with `@Inject lateinit var`.
- Compose screens take `onBack`/menu callbacks from MainActivity's NavHost; no bottom nav bar —
  navigation is the Google Messages layout (top bar + account menu + extended compose FAB).

## Tests (120 on JVM, all green)
- Robolectric 4.14.1 runs the REAL framework (Room+SQLite, resource tables, PhoneNumberUtils)
  — `unitTests.isIncludeAndroidResources = true` is required for resource lookups to work.
- **Robolectric gap**: `SmsManager.divideMessage()` is unimplemented in ShadowSmsManager, so
  `SmsSender.send` always fails there; SmsSenderTest asserts the clean-failure path instead.
  Real multipart send is untestable here — do not "fix" the app for it.
- Plain JVM unit tests touching `android.*` need `testOptions.unitTests.isReturnDefaultValues`.
- **Migration tests**: copy `createSql` verbatim from `app/schemas/.../<n>.json` — v2 `reactions`
  is `PRIMARY KEY(messageId, emoji)` with no `id`. Build the old schema with
  `FrameworkSQLiteOpenHelperFactory().create(...)` at the old version, seed, close, then open via
  `Room.databaseBuilder(...).addMigrations(...)` so Room's own `TableInfo` validation runs.
  Adding a migration means **every older migration test needs the full path**:
  `.addMigrations(MIGRATION_1_2, MIGRATION_2_3)`.
- **Time-based logic under Robolectric**: `runTest`'s auto-advance races Room's real threads and
  can silently expire a delay under test. Give the repo a private scheduler —
  `CoroutineScope(StandardTestDispatcher(TestCoroutineScheduler()))` — and drive it with
  `advanceTimeBy(...)` then `runCurrent()`. `SmsRepository` takes an optional `scope` param.
- Room + coroutine tests: point Room's own executors at the test dispatcher
  (`setQueryExecutor(dispatcher.asExecutor())`) or reads race writes; a `WhileSubscribed`
  StateFlow also needs an active collector in the test before it emits.
- Lint **MissingPermission** only accepts a permission check it can see *in the same method*
  as `manager.notify(...)`; one behind a helper is still flagged, and
  `areNotificationsEnabled()` alone is not accepted — inline
  `ContextCompat.checkSelfPermission(POST_NOTIFICATIONS)` is.
- Arabic locale renders digits as Arabic-Indic (٥ not 5) via resource formatting; assert
  `\u0665` rather than "5". Arabic plurals need **`zero`** or lint errors (`MissingQuantity`);
  `%d` + a word in a `<string>` must become `<plurals>` (`PluralsCandidate`).

## Gotchas that cost real time
- `material-icons-extended` holds only icons *missing* from `-core`, so a wrong guess fails
  to compile. `Icons.Filled.Unblock` does not exist → `LockOpen`. Directional glyphs
  (`ArrowBack`, `Send`, `Forward`, `TextSnippet`) → `Icons.AutoMirrored.Filled.*`.
  Verify with `unzip -l <icons jar> | rg 'filled/NameKt.class'` under
  `~/.gradle/caches/8.9/transforms/*/transformed/material-icons-*-release-api.jar`.
- `Intent.ACTION_RESPOND_VIA_MESSAGE` is not public API — use the string literal.
- `const val` of an enum value is illegal in Kotlin.
- `PhoneNumberUtils.formatNumber` can return null without throwing.
- In-app language switching = `AppCompatActivity` + `AppCompatDelegate
  .setApplicationLocales` + `android:localeConfig` + `Theme.AppCompat.DayNight.*`
  (the original `android:Theme.Material.*` parent is incompatible).
- `backup_rules.xml`: `<exclude domain="database" path="."/>` with no matching `<include>`
  is a lint **error** (`FullBackupContent`). To keep messages out of backups, include only
  the safe domain (the DataStore under `file`/`datastore/`) instead of excluding the rest.
- `android:icon="@mipmap/ic_launcher"` needs a non-adaptive fallback in `mipmap-*dpi`,
  not just `mipmap-anydpi-v26`, or the icon cannot resolve on some launchers.
- Notification small icons must be a plain white silhouette with **no** `android:tint`,
  otherwise they vanish on a light status bar.
- `stringResource()` cannot be called from `LazyListScope` (`items {}`); hoist it.
- SQLite `LIKE` treats `%` and `_` as wildcards — escape user input and add `ESCAPE '\'`.
- `DateFormat.SHORT` follows the device 12/24-hour setting; `SimpleDateFormat("HH:mm")`
  does not.

## Publishing releases
- Public repo **alifaleh302-dev/sms-messenger**; `GITHUB_PERSONAL_ACCESS_TOKEN` has full `repo`
  scope (create repo, push, release). Push via
  `git remote set-url origin https://${GITHUB_PERSONAL_ACCESS_TOKEN}@github.com/...`.
- `gh release create` returns **403 "Resource not accessible by integration"**. Use REST with
  `$GITHUB_PERSONAL_ACCESS_TOKEN`: `POST /repos/{o}/{r}/releases`, then
  `POST https://uploads.github.com/repos/{o}/{r}/releases/{id}/assets?name=`. To replace an
  asset, `DELETE .../releases/assets/{asset_id}` first — a duplicate name fails. The upload
  response carries `digest: sha256:...`, a free integrity check.
- Pattern: release tagged `vX.Y.Z` with assets `app-release.apk` + `SHA256SUMS.txt`, Arabic body,
  then re-download and confirm the digest matches the built artifact. README carries a
  "التحميل / Download" table with direct link + size + SHA-256; `/releases/latest` redirects.
- **The APK build is NOT byte-reproducible** — three runs gave three digests. Build once, upload
  *that* file, and put *that* digest in the README; verify by downloading the asset back and
  comparing to the uploaded file, never to a fresh local build. If the source changes after
  publishing, rebuild, re-upload, and update the README digest together.
- Build signed with `bash tools/gbuild.sh :app:assembleRelease` →
  `app/build/outputs/apk/release/app-release.apk`. Verify with
  `/opt/android-sdk/build-tools/35.0.0/apksigner verify --print-certs` and `aapt2 dump badging`
  (full build-tools path — neither is on PATH).
- Keystore is **sandbox-only**: `keystore/release.jks` + `keystore.properties`, both gitignored,
  alias `smsmessenger`, cert SHA-256 starts `b5fb4e50…`. Losing the workspace loses the key, so
  users would have to uninstall to take a future build. `signingConfigs.storeFile` resolves
  relative to the `app` module — use `rootProject.file(...)` for repo-root paths.

## Feature notes
- **v1.3.0**: smart + custom conversation tabs, undo send, app lock, OTP auto-delete (DB v3).
- Smart tabs are **derived from message content on every list update**, not stored, so a new
  message lands in the right tab immediately and the rule can be refined without a migration.
  Classification reads all bodies in one query (`MessageDao.allBodies()` →
  `ConversationBody`) and groups in memory; per-thread queries would be N per update.
- The undo-send hold stores the message **immediately** and delays only the network hand-off, so
  the bubble appears at once and `cancelPendingSend` is a plain delete.
- App lock: the no-PIN state offers "turn off app lock" rather than sending the user to
  Settings, because a lock that cannot be satisfied would otherwise be a permanent lockout.
- Long-press bubble → `MessageActionSheet` (reactions, reply, forward, copy, share, star,
  delete, select-more, details, copy-code). `VerificationCode.extract` only fires when the
  text contains a code word (EN or AR) *and* a 4–8 digit run not inside a longer run.
- Trash retention is 30 days, purged when the Trash screen opens.
- One reaction per message per side: same emoji toggles off, a different emoji replaces ours.
