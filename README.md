# Extension Aniyomi à partir d'un lien — tout automatique (sans rien installer)

Une fois, côté GitHub (5 min) :
1. Crée un compte GitHub, puis ouvre https://github.com/yuzono/anime-extensions et clique **Fork**.
2. Dans TON fork : **Add file → Upload files** et envoie le dossier `.github` et le dossier `tools` de ce zip
   (glisse-dépose ; commit directement sur la branche principale).
3. Onglet **Actions** de ton fork : clique sur « I understand my workflows… » pour activer les Actions.

À chaque nouveau site :
1. Actions → **Make extension from URL** → **Run workflow**.
2. Remplis : URL du site, nom, langue → **Run workflow**.
3. Attends ~5–15 min. Ouvre l'exécution terminée → section **Artifacts** :
   - `extension-apk` : l'APK à installer sur ton téléphone.
   - `generated-sources-and-report` : le code + `REPORT.md` (ce qui a été deviné / à corriger), même si la compilation échoue.

Si ça échoue ou si l'extension ne charge rien : le site est sûrement en JavaScript / Cloudflare. Envoie-moi `REPORT.md`
et le fichier `.kt`, je corrige le code.
