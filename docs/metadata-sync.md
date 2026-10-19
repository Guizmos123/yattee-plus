# Synchronisation autonome des métadonnées (YTP-META-03)

> Depuis META-04, les acquisitions linguistiques des fiches partagent le service,
> les baux et le budget ; voir [video-metadata-sharing.md](video-metadata-sharing.md).
> Les mentions META-04 ci-dessous décrivent le périmètre historique de META-03.

`server.lifespan` démarre `metadata_sync` indépendamment du Feed et attend son
arrêt. Le job ne découvre aucune vidéo et ne scanne aucune chaîne : il utilise
les vidéos YouTube de `cached_videos`, dédupliquées entre chaînes, pour les trois
variantes `original`, `fr`, `en`. Les vidéos sorties du cache ne sont plus
planifiées ; leurs observations restent conservées.

La boucle Feed conserve sa collecte et ses nettoyages. Son ancien appel automatique
à `enrich_feed_video_metadata()` est retiré. Les helpers historiques subsistent
pour compatibilité interne/tests, sans appelant actif dans les parcours serveur.
Les fiches à la demande restent sur leur chemin actuel : META-04 n'est pas activé.

## Réglages et interface

La migration additive `017` ajoute les réglages persistants suivants, les champs
techniques de planification et une ligne d'agrégats dans `metadata_sync_state`.
Elle ne modifie ni les textes historiques ni leurs dates de contrôle/acquisition.

| Paramètre | Défaut | Validation |
| --- | --- | --- |
| `metadata_sync_enabled` | `true` | booléen strict |
| `metadata_sync_interval_hours` | `24` | entier strict, 1–72 |
| `metadata_sync_max_http_per_hour` | `150` | entier strict, 1–10 000 |

La borne supérieure du budget protège contre une configuration accidentelle
excessive. Les trois paramètres suivent les GET/PUT administrateur existants et
les parcours initialisation/chargement/sauvegarde du frontend. Les contrôles
figurent dans **Feed Fetcher → Video metadata**, avec la navigation à neuf sections
inchangée. Les modifications non enregistrées restent dans le même composant.

L'intervalle est une cible de renouvellement par champ, pas un lancement massif
de toutes les vidéos chaque heure. Un champ acquis à T devient échu à T + intervalle.
Modifier l'intervalle recalcule cette échéance à partir de T, sans déplacer T.
Pour un ancien champ sans date réseau, `checked_at` sert uniquement de référence
de planification ; il n'est pas converti en preuve d'une acquisition réseau.
Pendant cette transition, ses échéances historiques sont conservées si elles
sont plus proches et son renouvellement ne dépasse pas 24 h, même si le nouvel
intervalle vaut 72 h. Cela évite un trou de fraîcheur avant la première observation
réseau réellement tracée, sans modifier la fenêtre d'affichage historique.

Une modification locale réveille immédiatement la boucle. Les autres processus
relisent la politique au plus tard après cinq secondes ; chaque tentative HTTP,
retries compris, relit également l'activation et le budget persistants. Les
requêtes en vol sont annulées lors d'un changement de politique détecté. Désactivé,
le job ne crée pas de travaux, ne nettoie rien et ne lance aucune acquisition ;
seule la surveillance de configuration reste active.

## Équité, échéances et reprise

Un lot contient au plus 30 variantes, avec deux acquisitions concurrentes. Le
motif de dix emplacements est : manquant, renouvellement, manquant, renouvellement,
manquant, reprise, manquant, renouvellement, manquant, renouvellement. Cela réserve
50 % aux variantes/champs manquants, 40 % aux renouvellements et 10 % aux échecs.
Les catégories vides prêtent leurs places. Le curseur est persisté lors de la
première tentative HTTP réellement admise de chaque acquisition, dans la même
transaction que le budget. Il ne revient pas au début du motif après un crash,
une interruption ou un budget d'une seule tentative par heure.

Les échéances les plus anciennes passent d'abord. À échéance égale, le FIFO
persistant et le rang par chaîne/langue distribuent le passage entre chaînes,
vidéos et variantes. Une vidéo partagée est affectée à sa première chaîne par
ordre lexical pour cette rotation, sans être dupliquée. Les arrivées nouvelles
ne passent pas devant les anciennes variantes manquantes déjà en attente.

Le backoff est persistant : 15 minutes, doublé après chaque acquisition incomplète
ou échouée, jusqu'à 24 heures. Une acquisition complète le réinitialise. Une
acquisition partielle conserve le texte/date du champ absent et son échéance ;
le backoff limite seulement la prochaine tentative, sans rendre ce champ frais.
Les refus techniques (bail occupé, SQLite occupé, budget, pause, désactivation)
ne sont pas enregistrés comme échecs réseau et ne font pas croître ce backoff.

Les échéances dérivent des dates conservées, et le backoff/cursor sont en SQLite :
un redémarrage ne réinitialise aucun de ces éléments. Les anciennes variantes
Feed en échec conservent leur délai de reprise à leur première mise en file.

## Coordination et réseau

Le job réutilise les baux de META-02, y compris pour un coordinateur de lot sous
la clé technique `internal/metadata-sync/original`. Son bail de 60 secondes est
renouvelé toutes les cinq secondes. Un seul processus pilote un lot ; les deux
acquisitions du lot conservent chacune leur propre bail de 60 secondes. Les
contrôles propriétaire/génération/expiration protègent toujours les écritures.
Une perte de bail ou un échec du renouvellement annule les travailleurs du lot.
Aucune transaction n'est ouverte pendant le réseau.

Le service META-02 borne chaque acquisition à 50 secondes. Son cache mémoire est
désactivé pour ce job : seuls les travaux échus sont choisis, et une ancienne
réponse partielle en mémoire ne doit pas masquer un champ encore manquant.
Il n'y a pas de nouvelle couche de retry, ni de fallback vers yt-dlp/Invidious.

Le plafond horaire est glissant et commun aux processus sur le même fichier.
Les retries consomment chacun une tentative ; les refus n'en consomment aucune.
Un 429 arrête les retries et crée la pause partagée META-02 selon Retry-After
(secondes/date HTTP, repli 60 secondes). Le backoff de la variante peut dépasser
cette pause ; après sa fin, les autres travaux peuvent avancer. Deux requêtes
déjà en vol peuvent terminer après le premier 429.

À 150 tentatives/heure, la capacité théorique est de 150 acquisitions réussies
sans retry, soit 50 vidéos avec trois variantes par heure, 1 200 vidéos par jour.
Avec trois tentatives par acquisition, elle tombe à 50 acquisitions/heure. Régler
l'intervalle sur une heure n'augmente pas ce plafond ; le retard reste visible.

Les limites META-02 restent applicables : admission durable avant envoi, donc un
crash peut consommer prudemment un slot sans HTTP émis ; absence de garantie
d'exécution unique du côté YouTube après une expiration ; horloges cohérentes et
verrouillage SQLite fiable indispensables. Le budget concerne les appels de ce
service, pas les acquisitions historiques des fiches, ni les exports de cookies.

## Lecture et fraîcheur linguistique

`get_feed_video_metadata()` restitue les nouvelles observations au sélecteur
commun, avec source et date par champ, sans réécrire `feed_video_metadata`.
Une observation historique contrôlée plus récemment gagne prudemment sur un
champ nouveau plus ancien. Aucun texte valide n'est effacé par une erreur.

L'adaptation à 72 heures est isolée : seuls les champs disposant d'une véritable
date réseau META-02 utilisent l'intervalle configuré comme fenêtre de fraîcheur.
Les variantes historiques gardent les 24 heures de LANG-02. Au-delà de la fenêtre,
un champ persistant reste seulement un fallback si le fournisseur n'a pas ce
champ : aucune politique globale stale-while-revalidate n'est introduite.
La préférence entre variantes et les règles des listes déjà localisées restent
inchangées. `hl` et `/player` ne prouvent jamais la langue réelle. `lang` absent,
les contrats JSON, les URLs de lecture et le fournisseur forcé sont inchangés.

## État administrateur et rétention

`GET /api/metadata-sync/status` requiert le même administrateur que Settings.
Le bouton « Refresh metadata job status » affiche les agrégats partagés :
activation, état actif/inactif/suspendu, dernière exécution, dernière réussite,
acquisitions réussies/échouées/partielles, variantes échues en attente, plus ancienne
échéance, HTTP consommés/limite, pause et prochaine admissibilité. Les réussites
incluent les réponses partielles, également comptées séparément. La prochaine
reprise est une échéance estimée, soumise à concurrence, au budget et au backoff.
Aucun corps HTTP, cookie, secret, URL privée ou identifiant d'authentification
n'est exposé. Le statut se rafraîchit explicitement sans écraser les brouillons.

Le journal HTTP conserve sept jours. Le nettoyage se fait normalement toutes les
heures, par transactions de 1 000 lignes maximum ; un retard est rattrapé par les
cycles suivants. La fenêtre horaire n'est jamais purgée, même pour une tentative
sans résultat après crash. Le nettoyage s'arrête si le job est désactivé/suspendu
et reprend ensuite. Les observations vidéo et les compteurs agrégés restent
conservés. Une grosse base peut donc demander plusieurs cycles de rattrapage.

META-04 devra mutualiser la lecture/acquisition des fiches à la demande avec ce
service et son budget, sans changer `lang` absent ni `invidious=true`. Les migrations
doivent être appliquées avant de lancer les workers ; éviter les migrations
concurrentes au déploiement et conserver une sauvegarde du volume SQLite. Aucun
changement Docker, dépendance ou scheduler externe n'est nécessaire ici.

## Inventaire du chantier

| Rôle | Fichiers |
| --- | --- |
| Nouveau scheduler et files | `metadata_sync.py`, `database/repositories/metadata_sync.py` |
| Raccord META-02 | `database/repositories/metadata_acquisitions.py`, `innertube/_metadata.py` |
| Cycle de vie et découplage Feed | `server.py`, `feed_fetcher.py` |
| Configuration et API administrateur | `settings.py`, `database/repositories/settings.py`, `routers/admin/settings.py` |
| Migration additive | `migrations/versions/017_metadata_sync.py` |
| Lecture et fraîcheur par champ | `database/repositories/feed_metadata.py`, `metadata_selection.py` |
| Contrôles et état Settings | `static/index.html`, `static/js/alpine/components/settings.js` |
| Tests Python | `tests/test_metadata_sync.py`, `tests/test_metadata_acquisitions.py`, `tests/test_feed_language.py`, `tests/test_channel_retention.py` |
| Tests frontend | `tests/test_settings_navigation.js` |
| Documentation | `docs/metadata-sync.md`, `docs/metadata-infrastructure.md` |

La migration 016, les types META-02, les routes publiques, le client HTTP,
les extracteurs historiques, les styles et les mécanismes de navigation UI-01
sont conservés. Aucun test existant n'est supprimé : seules l'attente de l'ancien
appel Feed et les assertions de schéma affectées par la migration sont adaptées ;
les tests frontend sont complétés.

## Validation réalisée

- 25 nouveaux cas Python ciblés : concurrence réelle entre processus, baux,
  activation, réveil de configuration, arrêt normal/exceptionnel, intervalles,
  transition historique, équité avec arrivées continues et budget minimal,
  plafond de 150 retries inclus, pauses, champs partiels, persistance et API.
- Suite Python complète : **1 300 réussites, 2 tests ignorés, 94 sous-tests réussis**.
  Dix avertissements déjà présents autour des mocks/coroutines et de Starlette.
- Neuf tests Node passent, dont navigation UI-01, rétention et nouveaux réglages.
- Chromium/Playwright, API simulée : 320/390/768/1 280 px, neuf sections,
  brouillons conservés, sauvegarde/rechargement, état affiché, aucun débordement
  horizontal ni erreur JavaScript.
- Ruff sur tous les fichiers Python du chantier et `git diff --check` : OK.

Les tests utilisent des bases temporaires. La suite complète est lancée après
`database.init_db()` sur un `DATA_DIR` temporaire, pour ne pas reproduire les
fixtures SQLite non initialisées déjà identifiées pendant LANG-02. Aucune base
de production n'est ouverte ou migrée, aucun build Docker/déploiement/commit.
