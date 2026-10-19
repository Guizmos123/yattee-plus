# META-04 : métadonnées des fiches et job partagé

Le raccord porte seulement sur les titres/descriptions `original`, `fr`, `en`
de `routers/videos.py`. L'extraction principale et ses appels nécessaires à la
lecture restent inchangés. Le service partagé ne lit ni ne réécrit les streams,
URLs, commentaires, sous-titres, recommandations ou playlists. Par arbitrage
explicite, les recommandations hybrides FR/EN conservent leur localisation
historique : leur appel `/next` et leur cache mémoire restent indépendants et
hors du raccord META-04. Les titres/descriptions de la fiche renvoyés par cet
appel ne sont jamais utilisés pour contourner le budget ou la sélection partagés.

## Réutilisation et sélection

- Sans `lang`, retour historique immédiat, sans lecture/acquisition linguistique.
- Avec `invidious=true`, sélection locale uniquement, sans acquisition supplémentaire.
- Sinon, lecture persistée avant toute acquisition. Chaque champ de la variante
  demandée est considéré présent s'il possède un texte admissible, chaîne vide
  comprise. Une variante originale de fallback ne constitue pas une variante FR/EN.
- La présence est indépendante de la fraîcheur : un champ ancien présent n'est
  pas réacquis par la fiche. Le sélecteur LANG-02/META-03 garde ses priorités et
  fenêtres (historique 24 h, nouvelles observations selon l'intervalle 1–72 h).
  Un texte trop ancien ne remplace donc toujours pas un champ fournisseur présent.
- Un champ réellement absent déclenche le service META-02. Après persistance,
  le même sélecteur est réappliqué aux observations, sans priorité artificielle
  accordée au simple fait d'avoir tenté une acquisition. Pas de nouvelle SWR.

Les dates restent des observations réseau, pas des dates de modification YouTube.
`hl` ne prouve aucune langue réelle. `/player` fournit seulement des candidats
originaux : sa description brute est utilisée, jamais la description fusionnée.

## Coordination et budget

Les fiches utilisent `MetadataAcquirer(on_demand=True, cache_ttl=0)` : pas de cache
mémoire pouvant masquer un champ absent. La réservation vérifie à nouveau les
champs et le backoff sous le même `BEGIN IMMEDIATE` que le bail, avant tout HTTP.
Le commutateur `metadata_sync_enabled` et InnerTube doivent être actifs pour une
nouvelle acquisition ; la lecture persistée reste disponible lorsqu'ils sont arrêtés.

Chaque tentative, retries compris, utilise le plafond persistant META-03 dans la
transaction d'admission. Les pauses 429, backoffs, générations et dates par champ
restent ceux de META-02/META-03. Une erreur ou un résultat partiel n'efface aucun
champ valide. Les refus techniques n'augmentent pas le backoff.

Un second lecteur n'acquiert pas le même bail : il attend au plus deux secondes,
en relisant SQLite hors transaction réseau, puis réutilise les observations ou
conserve son fallback fournisseur. Il ne réclame pas de nouveau bail dans cette
attente. Une réponse lente peut donc donner temporairement deux textes différents
aux deux lecteurs, mais aucun HTTP linguistique dupliqué.

Le job transmet la génération sélectionnée dans son lot. Si une fiche a acquis
la variante depuis cette sélection, il ignore cette entrée devenue obsolète. Ce
contrôle protège aussi le cas où la fiche a déjà terminé et libéré son bail.
Les fiches ne font pas avancer le curseur d'équité des lots du job. Leurs succès,
échecs et backoffs sont comptabilisés par le même mécanisme ; les agrégats de succès
deviennent partagés, tandis que `last_run` reste une exécution du job.

## Vidéos hors Feed : admission bornée, sans purge

Le nettoyage historique de `feed_video_metadata` suit les références du Feed.
Les observations nouvelles restent dans `metadata_acquisitions`, qu'il ne supprime
pas. Pour éviter une croissance non bornée avec des identifiants de fiches nouveaux,
la migration additive **018** ajoute `on_demand_entry`, par défaut à zéro : toutes
les données préexistantes restent protégées et ne consomment aucun nouveau slot.

Seules les lignes créées par une fiche, sans référence Feed ni variante historique
correspondante, portent ce marqueur. Leur admission est limitée atomiquement à
**3 000 variantes supplémentaires** au total, soit environ 1 000 vidéos avec les
trois variantes. C'est une borne cumulative conservatrice, pas un TTL ni un LRU.
Les slots ne sont pas recyclés, même après une erreur : aucune donnée historique
ou nouvelle n'est effacée. À saturation, aucun nouvel HTTP n'est déclenché pour
une telle variante ; le fournisseur reste le fallback. Les variantes déjà admises
et les références Feed restent utilisables. Une politique d'éviction nécessiterait
un choix ultérieur explicite ; elle n'est pas introduite ici.

Le journal HTTP garde sa rétention META-03 de sept jours. Le job assure désormais
sa maintenance aussi lorsqu'il est suspendu par le budget/429, pour que le trafic
des fiches ne puisse pas affamer ce nettoyage existant. Aucune nouvelle purge
de métadonnées n'est ajoutée ou exécutée. Désactivé, le service n'admet aucun HTTP
linguistique et n'alimente donc pas ce journal.

## Limites et validation Synology

Le plafond concerne les acquisitions linguistiques partagées de la fiche, pas
les appels de l'extraction principale nécessaires à la lecture, ni les appels
historiques de localisation des recommandations. Ces derniers peuvent subsister
même avec une variante de fiche déjà persistée ou un budget partagé épuisé.
Sur une première fiche hybride FR/EN avec recommandations, deux appels `/next`
peuvent donc être nécessaires : le service partagé pour les champs absents et
l'ancien parcours pour les recommandations. Leur mutualisation est hors périmètre.
Un trafic de fiches soutenu
peut retarder les lots ; aucun budget séparé ni refonte du scheduler n'est ajouté.
Les limites des baux META-02 subsistent : une admission avant un crash peut
consommer un slot sans envoi et l'exécution externe ne peut être exactement unique.

Sur Synology, vérifier le même volume SQLite effectif entre workers, les verrous
du système de fichiers, les horloges, la latence des requêtes avec un bail occupé,
les pauses/reprises et les URLs de lecture réelles. Appliquer la migration avant
les workers, avec sauvegarde et démarrage des migrations sérialisé. Aucun build
Docker, déploiement ou commit n'est effectué par ce chantier.

## Fichiers et validation

Les changements concernent `routers/videos.py`, `innertube/_metadata.py`,
`metadata_types.py`, `metadata_sync.py`, `database/repositories/metadata_acquisitions.py`,
`database/repositories/metadata_sync.py`, `migrations/versions/018_on_demand_metadata.py`,
`tests/test_video_language.py`, `tests/test_video_language_http.py`,
`tests/test_video_metadata_shared.py`, `tests/test_localized_recommendations.py`,
`docs/metadata-sync.md` et cette documentation. Aucun test existant n'est supprimé.

Les 356 tests ciblés initiaux passent. L'unique suite complète a donné
1 320 réussites, 2 tests ignorés et 3 échecs sur les recommandations ; après
l'arbitrage utilisateur, leurs attentes historiques sont conservées et les
tests concernés sont relancés avec les tests des fiches et du service partagé :
**88 réussites et 31 sous-tests réussis**. Ruff sur les fichiers Python du chantier
et `git diff --check` passent également.
La suite complète n'est pas exécutée une seconde fois, conformément à la demande.
Toutes les bases utilisées sont temporaires ; aucune purge ni migration d'une
base de production n'est effectuée.
