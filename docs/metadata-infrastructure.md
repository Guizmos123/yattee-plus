# YTP-META-02 : infrastructure préparée

> État historique de META-02. Depuis META-03, le job autonome utilise cette
> infrastructure ; les raccords actifs et les paramètres sont décrits dans
> [metadata-sync.md](metadata-sync.md). Les fiches à la demande restent inchangées.

Le service `innertube._metadata.MetadataAcquirer` est volontairement sans appelant
de production. Le Feed, les fiches vidéo, les listes et leurs limites actuelles
restent sur leurs chemins existants. Les appels historiques ne sont donc pas
encore coordonnés ou comptés par cette infrastructure. Aucun scheduler, paramètre
Settings, endpoint ou fournisseur de fallback supplémentaire n'est activé.

## Migration et intégrité

La migration additive `016`, appliquée par le mécanisme Alembic habituel au
prochain démarrage, ajoute trois tables :

- `metadata_acquisitions` : bail par `(site, video_id, language)`, génération,
  observations titre/description avec source/date propres et dernier résultat.
- `metadata_http_attempts` : journal persistant des tentatives admises, statut HTTP
  réel, erreur typée, Retry-After et dates de début/fin.
- `metadata_provider_pauses` : échéance partagée de pause InnerTube.

Aucune ligne existante n'est transformée, supprimée ou antidatée. Les anciennes
variantes restent dans `feed_video_metadata` et continuent d'être lues comme
auparavant. `MetadataStore.stored_fields()` peut les restituer comme fallback,
avec date réseau inconnue (`None`) ; `checked_at` n'est pas converti en date réseau.
Ce lecteur n'affirme pas leur fraîcheur ni leur langue réelle. Le futur raccord
aux sélecteurs devra conserver leurs règles d'admissibilité.

Les observations du nouveau service restent dans la nouvelle table, séparées des
dates de contrôle du Feed et de son nettoyage des variantes orphelines. Le raccord
de ces observations à l'affichage fait partie des étapes suivantes. Un résultat
partiel ne met à jour que les champs présents (chaîne vide comprise), avec leur
provenance et leur date réseau. Un résultat plus ancien ou à date égale ne remplace
pas un champ déjà observé. Une erreur conserve tous les textes valides. Un hit du
cache mémoire conserve les dates initiales, ne compte aucun HTTP et n'écrit rien.

Le résultat interne contient l'identité, les champs effectivement obtenus, leurs
sources/dates, l'origine mémoire/réseau, le résultat complet/partiel/échec, l'erreur,
le statut HTTP éventuel et Retry-After. `acquired_at` est la fin de la dernière
tentative réseau connue, y compris échouée ; ce n'est jamais une date de modification
YouTube. Les champs possèdent leur propre date d'acquisition réussie.

`hl=fr/en` exprime une préférence. Le titre et `shortDescription` bruts de
`/player` restent des candidats originaux incertains. Le service ne réutilise
jamais une description fusionnée du lecteur comme originale vérifiée.

## Baux et budget

Chaque réservation utilise une transaction courte `BEGIN IMMEDIATE`, un UUID et
une génération croissante. Bail par défaut : 60 secondes. La génération, le
propriétaire et l'expiration sont vérifiés avant chaque tentative HTTP et avant
toute sauvegarde/libération. Un ancien propriétaire ne peut ni écrire, ni libérer
le bail de son successeur. Un crash est récupéré à l'expiration. Le service borne
l'opération complète à 50 secondes, setup et retries compris, puis annule le réseau.

Aucune connexion/transaction SQLite ne traverse un `await` réseau. L'attente SQLite
est bornée à 250 ms par opération (configurable de 0 à 1 seconde pour ce composant).
`SQLITE_BUSY/LOCKED` devient une erreur interne explicite ; aucun retry SQLite
illimité. Si la libération est impossible, l'expiration reste le filet de reprise.

`hourly_limit=None` ne définit aucun nouveau plafond actif. Une valeur positive
prépare un plafond glissant sur 3 600 secondes, partagé entre les processus utilisant
le même fichier SQLite et la même politique de plafond. Admission et comptage sont
atomiques ; une nouvelle instance ou un redémarrage ne remet pas le journal à zéro.
Chaque retry consomme sa propre entrée, immédiatement avant `client.post`. Une
admission refusée ne consomme rien.

L'envoi externe et le commit SQLite ne peuvent pas être atomiques : un crash entre
l'admission durable et l'envoi peut consommer prudemment un slot sans requête émise.
Une entrée sans fin représente un résultat inconnu, pas une réussite. Un bail
expiré ne garantit pas qu'un serveur distant ait cessé de traiter l'ancien HTTP ;
la génération protège les écritures locales, sans promesse d'exécution réseau unique.

Les retries restent uniquement dans le client existant (deux retries au plus,
délais 1 puis 2 secondes, codes retryables historiques). Sur le nouveau chemin,
429 interrompt immédiatement les retries et prolonge la pause partagée selon
Retry-After, en secondes ou date HTTP ; valeur absente/invalide : 60 secondes.
Une erreur JSON 429 sous HTTP 200 conserve le statut HTTP réel 200 et déclenche
également la pause. Les autres appels déjà en vol ne sont pas annulés par cette
pause. Aucun fallback yt-dlp/Invidious n'est lancé.

Le service réutilise le client HTTP avec son authentification, ses proxies et son
egress. Il fournit un contexte explicite avec la version déjà connue ou la version
de repli habituelle, sans GET caché de page d'accueil. Les redirections sont
refusées sur ce seul chemin pour éviter des requêtes non comptées. Le cache dédié
est borné à 200 entrées, TTL 3 600 s configurable au constructeur ; les caches
historiques sans date réseau fiable ne sont pas importés. Rien n'est instancié au
démarrage actuel. Les appels réseau indirects éventuels d'un export de cookies
navigateur par yt-dlp restent hors du budget HTTP de métadonnées InnerTube.

## Raccords META-03 et META-04

1. Créer une instance réutilisée par processus avec `MetadataStore` sur le volume
   SQLite partagé, un plafond horaire cohérent pour tous les appelants, et le TTL
   issu des paramètres existants. Les processus doivent partager la même horloge.
2. META-03 : brancher le futur job, puis remplacer explicitement l'enrichissement
   historique du Feed pour éviter deux producteurs dont un non coordonné. Ajouter
   intervalle, priorité équitable, backoff persistant par variante, concurrence,
   suivi administratif et critères de sélection ; ils ne sont pas activés ici.
3. Définir la lecture des observations persistées avec les sélecteurs LANG-02,
   sans prétendre dater une modification YouTube. Leur fenêtre actuelle de 24 h
   reste incompatible avec la garantie d'affichage d'une variante fraîche si le
   futur intervalle atteint 72 h ; aucune règle d'affichage n'est changée ici.
4. META-04 : lecture persistée d'abord, acquisition mutualisée uniquement lorsque
   nécessaire. Conserver `lang` absent et `invidious=true` sur leurs contrats.
5. Définir ensuite une rétention bornée du journal, toujours au-delà de la fenêtre
   horaire et des besoins de diagnostic. Aucune purge n'est introduite ici ; le
   journal croîtra avec l'utilisation future du service.

Le volume SQLite doit être partagé réellement entre processus sur un système de
fichiers avec verrous SQLite fiables. Des bases distinctes ne partagent ni les
baux, ni les compteurs. Aucun mode WAL ni changement Docker n'est imposé. Prévoir
une sauvegarde et éviter les démarrages concurrents exécutant les migrations au
déploiement futur, conformément au fonctionnement Alembic existant.
