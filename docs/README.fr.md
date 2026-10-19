# Documentation du code

Comment le bot est construit, pour qui doit le modifier. Pour ce qu'il fait et comment le lancer, voir le [README racine](../README.md). An English version is available: [README.en.md](README.en.md).

## Sommaire

1. [Carte des modules](#carte-des-modules)
2. [Demarrage](#demarrage)
3. [La tache, vue par le bot](#la-tache-vue-par-le-bot)
4. [Discord → GitHub](#discord--github)
5. [GitHub → Discord](#github--discord-1)
6. [Le formulaire de taches](#le-formulaire-de-taches)
7. [Le lien entre un post et une tache](#le-lien-entre-un-post-et-une-tache)
8. [Boucles et echos](#boucles-et-echos)
9. [Etat en memoire](#etat-en-memoire)
10. [Notifications](#notifications)
11. [Operations GraphQL GitHub](#operations-graphql-github)
12. [Gestion des erreurs](#gestion-des-erreurs)
13. [Tests](#tests)
14. [Limites et pistes](#limites-et-pistes)

---

## Carte des modules

| Module | Role | Importe Discord ? |
| --- | --- | --- |
| [`config.py`](../bot_horizon/config.py) | `Settings`, dataclass figee construite depuis l'environnement | non |
| [`sync.py`](../bot_horizon/sync.py) | Lecture et mise en forme pures : lire un post, lire un item du Project, corps d'issue, dates | non |
| [`notifications.py`](../bot_horizon/notifications.py) | Le contenu d'une card de notification et de la fiche d'un post, dans n'importe quelle langue | non |
| [`languages.json`](../bot_horizon/languages.json) | Les mots des cards et du formulaire, un objet par langue | non |
| [`github_client.py`](../bot_horizon/github_client.py) | Client GraphQL, une methode par operation | non |
| [`form.py`](../bot_horizon/form.py) | Le formulaire : la carte partagee, son calendrier, ses fenetres | oui |
| [`main.py`](../bot_horizon/main.py) | `HorizonBot` : handlers d'evenements, boucle de sondage, etat en memoire, actions du formulaire | oui |

Ce decoupage compte : tout ce qui est hors de `main.py` et `form.py` se teste sans connexion Discord, et `sync.py` comme `notifications.py` ne font aucune entree-sortie. Une nouvelle logique de lecture ou de mise en forme va la, pas dans un handler. `form.py` ne fait que recueillir la saisie : toute ecriture passe par une methode de `HorizonBot`.

## Demarrage

`run()` dans `main.py` charge le fichier nomme par `HORIZON_ENV_FILE` (`.env` par defaut, `.env.dev` avec `run.ps1 -Dev`), construit `Settings`, instancie `GitHubClient` puis `HorizonBot`, et se connecte.

- `setup_hook` enregistre la `FormView` de la carte du formulaire, pour que ses composants repondent encore apres un redemarrage.
- `on_ready` demarre la boucle de sondage en tache de fond, une seule fois : une reconnexion Discord redeclenche `on_ready`, et le garde `if self.poll_task is None` evite une deuxieme boucle. Il redessine aussi la carte du formulaire trouvee dans les 50 derniers messages du salon, ou la poste.

`Settings.from_environment()` echoue tot : sans `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `GITHUB_TOKEN` ou `GITHUB_PROJECT_ID`, l'erreur arrive avant la connexion. Toutes les autres variables ont une valeur par defaut, donc le bot tourne avec ces quatre-la.

## La tache, vue par le bot

Les deux cotes sont ramenes au meme dictionnaire, par `discord_task_data()` pour un post et `github_item_data()` pour un item du Project :

```python
{
    "title": "Fix login",
    "status": "Backlog",          # l'un de STATUS_NAMES
    "priority": "Low",            # l'un de PRIORITY_NAMES
    "assignees": ["alice"],       # logins GitHub
    "assignee": "alice",          # les memes, joints pour l'affichage
    "description": "…",           # le corps de l'issue sans le marqueur ni la ligne Source
    "size": "M",                  # une option du champ de taille, ou ""
    "start_date": "2026-10-20",   # dates ISO, ou ""
    "target_date": "2026-10-31",
    "closed": False,              # l'etat de l'issue
    "url": "https://github.com/…",
}
```

On en tire deux tuples :

- `task_state()` — titre, statut, priorite, assignes, taille et les deux dates : **ce qu'une notification annonce**. `state_changes()` transforme deux tuples en triplets `(cle, avant, apres)`, qu'une card rend dans sa propre langue.
- `card_state()` — la meme chose plus la description et l'etat ferme : **ce qu'un post affiche**. Une difference la reecrit le post, annoncee ou non. `same_card()` compare deux de ces tuples sans tenir compte de la casse, car GitHub rend un login dans sa propre casse.

Lecture d'un post : `thread_task()` prend le titre dans le nom du post, et le statut et la priorite dans le **premier tag de forum correspondant**, avec `Backlog` et `Low` par defaut. `Assigned to:` et `Description:` sont lus dans le message d'ouverture par des regex insensibles a la casse, et une liste d'assignes accepte `,`, `;`, `/` ou l'espace comme separateur, avec un `@` optionnel.

Lecture d'un item du Project : le statut vient du champ single-select `Status` ; la priorite d'un label d'issue portant un nom de priorite, sinon du champ de priorite configure ; les assignes des vrais assignes de l'issue, sinon du champ texte configure ou d'un champ nomme `Assigned to` ; la taille et les dates des champs nommes dans `GITHUB_SIZE_FIELD_NAME`, `GITHUB_START_DATE_FIELD_NAME` et `GITHUB_TARGET_DATE_FIELD_NAME` — un champ du Project d'abord, puis un champ d'issue de l'organisation (`Issue.issueFieldValues`), que GitHub garde a part et que seul `updateIssueFieldValue` ecrit.

Le corps d'issue que le bot ecrit est `GITHUB_MARKER`, la description et une ligne `Source:` (`issue_body()`). `issue_description()` en relit la description, et la relit aussi dans les corps ecrits par les anciennes versions, qui portaient des lignes `Status:`, `Priority:` et `Assigned to:` avant un titre `Description:`. `with_description()` remplace la description d'un corps en gardant son marqueur et son lien.

## Discord → GitHub

Chaque handler est garde par `is_configured_channel()` ou `is_post()`, qui acceptent le salon forum et ses posts (`parent_id`).

### `on_message` — creation

Un message du canal de notification est traite en premier, comme une commande, et ne devient jamais une tache. Dans le forum, les messages de bot sont ignores, et une reponse dans un post devient un commentaire (voir plus bas). Reste le message d'ouverture d'un post — celui dont l'id est l'id du thread — qui passe par la creation : le contenu vide est ignore, puis `create_issue()` est appele, qui :

1. cree une vraie issue dans `GITHUB_REPOSITORY_ID` (`createIssue`) ;
2. ajoute un label de priorite, en le creant avec une couleur si le depot ne l'a pas ;
3. assigne les logins connus, en ignorant et journalisant les inconnus ;
4. ajoute l'issue au Project (`addProjectV2ItemById`) — si le workflow d'ajout automatique du Project est passe avant, il reprend l'item existant ;
5. renseigne le `Status` du Project, le champ de priorite du depot, la taille et les dates ;
6. ferme l'issue si le post a ete ouvert avec le tag `Done`.

Le corps de l'issue commence par `GITHUB_MARKER` : c'est ainsi que le sondage reconnait plus tard une tache venue de Discord, a ne pas republier. L'url de l'issue est enregistree tout de suite dans `known_github_sources`, pour qu'une reponse postee avant le sondage suivant retrouve deja son issue.

### `on_message` — les reponses deviennent des commentaires

`is_post_reply()` reconnait un message poste dans un post du forum apres celui qui l'ouvre. `comment_on_github()` le passe a `comment_from_discord()`, qui retrouve l'item lie avec `find_linked_item()` — par la cle suivie ou la ligne `Source:`, jamais par le titre, car une reponse sur la mauvaise issue est pire qu'une reponse non copiee — et ajoute un commentaire (`addComment`), puis l'annonce par une card `COMMENTED`. Un draft item n'a pas de conversation : une reponse dessus est seulement journalisee.

Le corps du commentaire est construit par `discord_comment_body()` : `GITHUB_MARKER`, le nom de l'auteur avec un lien vers le message Discord, le contenu avec les mentions resolues (`clean_content`), et un lien par piece jointe.

### `on_raw_message_edit` et `on_raw_message_delete` — messages modifies et supprimes

Les evenements bruts arrivent meme pour un message que le cache ne contient plus, apres un redemarrage par exemple. Une modification dont le contenu et les pieces jointes n'ont pas change — Discord en signale une quand il deplie un lien — est ignoree.

- Une **reponse** modifiee reecrit son commentaire : `edit_comment_from_discord()` lit les commentaires de l'issue et retrouve celui que le bot a ecrit depuis ce message, grace au lien vers le message qu'il contient (`comment_of_message()`).
- Une **reponse** supprimee supprime ce commentaire de la meme facon, via `delete_comment_from_discord()`.
- Le **message d'ouverture** modifie d'un post ouvert a la main passe par `sync_opening_message()` : la description et les assignes qu'il contient maintenant sont ecrits sur l'issue (`update_linked_task()`), et un changement d'assignes est annonce.

Les messages de bot sont ignores, comme la suppression d'un message que le bot a supprime lui-meme (`messages_deleted_by_bot`).

### `on_thread_update` — modification

Declenche quand un post est renomme, retague, archive, epingle ou passe en mode lent. Le handler :

1. ecarte l'echo d'une edition que le bot vient de faire (voir [Boucles et echos](#boucles-et-echos)) ;
2. compare `thread_task(before)` et `thread_task(after)` — nom et tags — et **sort si aucun champ suivi n'a change**, ce qui empeche un archivage ou une epingle d'ecrire sur GitHub ;
3. appelle `update_issue_from_discord()`, qui met a jour le titre, remplace le label de priorite, renseigne le statut du Project et le champ de priorite natif, et rend la cle de l'item ;
4. quand le tag `Done` arrive, ferme l'issue et archive le post ; quand il part, rouvre l'issue ;
5. enregistre la tache telle qu'elle est maintenant dans `github_writes_from_discord`, puis notifie.

Les posts ouverts par le bot sont traites comme les autres : c'est le garde d'echo, et non l'auteur du message d'ouverture, qui empeche le bot de repondre a ses propres editions.

### `on_thread_delete` — suppression

Ignore les suppressions faites par le bot lui-meme, puis `delete_task_from_discord()` retrouve l'item lie et supprime l'issue, en se rabattant sur le retrait de l'item du Project si l'issue ne peut pas etre supprimee. Un draft item ne voit que son item de Project supprime. La cle est ensuite oubliee localement pour que le sondage suivant n'y voie pas une suppression cote GitHub.

## GitHub → Discord

`poll_github()` tourne indefiniment, toutes les `GITHUB_POLL_INTERVAL_SECONDS`, en appelant `poll_once()` :

```
list_items()  →  {cle: item}       cle = url de l'issue, ou id de l'item pour un draft
│                                    lu par pages de 100 items, jusqu'a 50 pages
├─ premier tour (known_github_items vide)
│     reconstruire les liens manquants, enregistrer chaque tache comme reference,
│     n'annoncer rien
│
├─ cle jamais vue
│     l'enregistrer
│     └─ pas de GITHUB_MARKER et pas de ligne Source → publier un post,
│        reecrire le lien sur GitHub, annoncer une creation
│
├─ cle connue, card_state different                    apply_github_change()
│     ├─ correspond a une ecriture faite depuis Discord → avaler l'echo
│     ├─ issue tout juste fermee → statut Done ; rouverte en etant Done → In progress
│     └─ sinon → reecrire le post (nom, tags, fiche, etat archive),
│        annoncer ce que task_state montre de change
│
├─ chaque cle : commentaires                           relay_github_comments()
│     nouveaux → les poster ; modifies → reecrire leur copie ; supprimes → la supprimer
│     (les commentaires avec GITHUB_MARKER viennent de Discord et ne sont pas touches)
│
└─ cle absente de la liste
      supprimer le post, oublier la cle, annoncer une suppression
```

Le premier tour n'annonce rien volontairement : il prend l'etat GitHub courant comme reference. Sans cela, toutes les taches existantes seraient republiees dans Discord a chaque redemarrage.

### Le post d'une tache

`refresh_post()` aligne un post sur sa tache. Il ne renomme et ne retague le post que si sa signature differe, et pour un post ouvert par le bot (`owns_post()`, d'apres le createur du thread) il reecrit le message d'ouverture avec `post_card()` quand la fiche differe. Un post ouvert a la main garde son message d'ouverture : le bot ne peut pas modifier un message qu'il n'a pas ecrit.

Un post archive ne peut pas etre modifie : `refresh_thread()` le desarchive le temps de l'edition et le rearchive ensuite ; une fermeture ou une reouverture fixe l'etat d'archivage final. `publish_to_discord()` ouvre un nouveau post avec la meme fiche, et exige au moins un tag de forum correspondant au statut ou a la priorite ; sans aucun, il leve une erreur, qui est journalisee, et la tache est retentee au sondage suivant.

### Ferme ↔ Done

Une issue fermee est une tache `Done`. `apply_github_change()` passe en `Done` une tache fermee sur GitHub et archive son post ; une tache rouverte en etant encore `Done` repasse en `In progress` et son post revient. Cote Discord, `on_thread_update` ferme l'issue quand le tag `Done` arrive et la rouvre quand il part. Archiver un post a la main ne change rien sur GitHub : Discord archive aussi de lui-meme les posts inactifs.

### Commentaires

`list_items()` ramene les 100 derniers commentaires de chaque issue, avec leur nombre total. `relay_github_comments()` les compare a `known_github_comments`, qui garde le corps et l'url de chaque commentaire deja vu :

- un nouveau commentaire est poste dans le post lie, mis en forme par `github_comment_message()` et coupe aux 2000 caracteres d'un message Discord, avec les mentions desactivees pour qu'un commentaire GitHub ne puisse pas pinger le serveur, et annonce par une card `COMMENTED` ;
- un commentaire modifie reecrit sa copie, et un commentaire supprime la supprime. `relayed_message()` retrouve la copie grace a `relayed_comments` tant que le bot tourne, et apres un redemarrage grace au lien du commentaire qui termine la copie. Un commentaire absent ne compte comme supprime que si la liste contient tous les commentaires de l'issue (`github_item_has_all_comments()`).

Le premier tour ne fait qu'enregistrer les commentaires, pour qu'un redemarrage ne reposte pas une conversation ; une tache vue pour la premiere fois plus tard voit ses commentaires existants copies dans son nouveau post.

## Le formulaire de taches

`form.py` contient les composants Discord ; `HorizonBot` contient ce qu'ils font.

```
FormView (LayoutView, permanente, custom ids horizon:card:*) : un Container
├─ titre : Nouvelle tache, ou Modifier une tache avec le lien du post
├─ ChannelSelect des posts du forum : vide → nouvelle tache, un post → charger sa tache
├─ Titre, Description : une Section chacun, avec le bouton Modifier qui ouvre sa fenetre
├─ statut, priorite, taille, assignes : un titre et une liste chacun
├─ dates de debut et d'echeance : une Section chacune, avec le bouton Choisir
├─ pied : ce qui s'est passe, qui a utilise la carte en dernier
└─ Creer/Enregistrer · Annuler · Coller un lien

calendrier (la meme carte, le temps de choisir une date)
├─ la date et son mois
├─ Mois precedent · Mois suivant · Effacer · Retour
└─ deux listes de jours, 1-16 et 17-fin, chacun avec son jour de la semaine
```

La carte est un seul message partage par toute l'equipe : c'est pourquoi c'est une seule `FormView` gardee par le bot (`HorizonBot.form_view`), enregistree dans `setup_hook` pour que ses custom ids fixes repondent encore apres un redemarrage. Un clic ou une fenetre repond en modifiant la carte (`show()`) ; un enregistrement ou un post charge apres un report modifie directement le message (`redraw()`), ce qu'un bot peut faire sur son propre message a tout moment.

C'est une `LayoutView`, les composants v2 de Discord : une carte reunit texte, listes et boutons, jusqu'a 40 composants et 4000 caracteres, ce que la carte respecte quelle que soit la longueur de la description. Les noms des mois et des jours viennent de `languages.json` (`months`, `weekdays`).

Discord n'a pas de champ texte dans un message. Les boutons Modifier et Coller un lien ouvrent une `FieldModal`, une fenetre d'un seul champ rempli avec la valeur actuelle ; l'envoyer appelle `FormView.write()`, qui remplit la carte, ou charge le post d'un lien. Creer sans titre ouvre directement la fenetre du titre. Un lien qui ne mene a aucun post en donne la raison dans le pied.

- `form_options()` donne a la carte ses choix — les statuts et priorites, les options du champ de taille, les logins assignables du depot, les champs de date presents dans le Project. Ils sont lus a la mise en place de la carte et gardes 10 minutes. Un champ absent du Project n'apparait pas dans la carte.
- `create_task_from_form()` ouvre le post avec ses tags et sa fiche, cree l'issue avec tous les champs et la ligne `Source:`, puis ajoute le lien de l'issue a la fiche. Si l'issue ne peut pas etre creee, le post est supprime.
- `load_task_for_form()` lit sur GitHub la tache d'un post, et refuse un post hors du forum (`TaskError("not_a_task")`) ou sans issue (`TaskError("task_not_found")`).
- Enregistrer relit la tache sur GitHub et ne lui applique que `changed_fields()` — ce que l'utilisateur a change par rapport aux valeurs chargees — pour qu'un champ non touche ne puisse pas defaire un changement fait sur GitHub entre-temps. `save_task_from_form()` ecrit ensuite ces champs (`update_task()`), ferme ou rouvre l'issue quand `Done` arrive ou part, met le post a jour, enregistre l'ecriture comme un echo a avaler, et annonce les changements. La carte revient a une nouvelle tache vide, avec le resultat dans son pied.

`ensure_form_panel()` redessine la carte du lancement precedent en nouvelle tache vide, ou la poste si le salon ne l'a pas, et supprime les panneaux des versions precedentes, dont plus rien ne reconnait les custom ids.

`TaskError` porte la cle de son message dans `languages.json`, avec les valeurs a y placer, que la carte affiche dans son pied. Tout autre echec est journalise et affiche comme `failed`, la tache restant dans la carte. Une date de debut choisie apres l'echeance, ou l'inverse, est refusee avec `dates_order`.

## Le lien entre un post et une tache

Le lien durable est la ligne `Source:` du corps de la tache GitHub, qui contient l'URL du post Discord. `discord_thread_id()` en extrait l'id du post.

- Une tache creee depuis Discord ou depuis le formulaire la porte des le depart, puisqu'elle fait partie du corps.
- Une tache creee sur le Project la recoit juste apres sa publication : `set_discord_source()` reecrit le corps de l'issue (`updateIssue`) ou du draft (`updateProjectV2DraftIssue`).

La liste des items du Project est en retard sur GitHub, de plusieurs minutes parfois apres l'ajout d'une issue. `find_linked_item()` lit donc une cle d'issue directement sur l'issue (`item_of_issue()`, via `resource(url:)` et `Issue.projectItems`), qui n'a pas ce delai, et ne se rabat sur la liste que pour un draft ou une issue hors du Project.

Comme il vit dans GitHub, ce lien survit a un redemarrage. Trois secours couvrent les cas ou il n'existe pas encore :

1. **`reconcile_links()` au demarrage** — pour chaque tache sans ligne `Source:`, il cherche parmi les posts du forum (actifs *et* archives) celui dont le nom correspond exactement au titre de la tache. Le bot nommant les posts d'apres le titre et maintenant les deux synchronises, une correspondance exacte identifie la bonne paire. Deux posts de meme nom laissent la tache sans lien, avec une ligne de log, plutot que d'en deviner un. Une tache qu'aucun post ne correspond n'a jamais ete publiee — elle etait au-dela des 100 items que lisait la version 1, ou la avant le premier lancement du bot — et `catch_up()` la publie, sans card de notification. Il ne le fait que si les posts archives ont pu etre listes, car une archive manquante ferait passer des taches publiees pour non publiees.
2. **`known_github_sources`** — la table en memoire, qui couvre la fenetre entre une creation et le sondage qui la voit pour la premiere fois.
3. **Le nom du post** — `find_linked_item()` accepte un titre en dernier recours, et ne s'y fie que si une seule tache correspond.

## Boucles et echos

Chaque cote ecrit chez l'autre, donc chaque ecriture peut revenir sous forme d'evenement. Un garde par echo :

| Garde | Echo qu'il supprime |
| --- | --- |
| `is_bot_message()` | Les messages du bot, modifications comprises |
| `threads_deleted_by_bot` | Le `on_thread_delete` d'un post que le bot vient de supprimer |
| `messages_deleted_by_bot` | Le `on_raw_message_delete` d'une copie que le bot vient de supprimer |
| `threads_edited_by_bot` | Le `on_thread_update` d'un post que le bot vient de renommer ou retaguer |
| `github_writes_from_discord` | Un changement de tache pousse depuis Discord, revenu a un sondage suivant |
| `is_bot_comment()` | Un commentaire que le bot a ecrit depuis une reponse Discord, revu au sondage suivant |

Le garde des posts retient **ce que le bot s'apprete a ecrire**, et n'ecarte l'evenement entrant que s'il correspond exactement :

```python
# avant d'editer un post
signature = (name, frozenset(tag.name for tag in tags))
if thread_signature(thread) == signature:
    return                                    # deja correct, ne pas editer du tout
self.threads_edited_by_bot[thread.id] = signature
```

```python
# a la reception d'une mise a jour de post
if self.threads_edited_by_bot.pop(after.id, None) == thread_signature(after):
    return                                    # c'est notre propre edition
```

Comparer une signature plutot qu'un simple id a son importance : si un utilisateur change autre chose entre-temps, l'evenement ne correspond plus et il est traite normalement. Une edition qui echoue retire son entree pour ne pas avaler un changement ulterieur, et un post deja dans l'etat vise n'est jamais edite, ce qui supprime l'evenement a la source.

Le garde GitHub garde, par tache, une courte liste des etats que le bot a ecrits (`expect_github()`, construit sur `latest_details()`). `is_own_write()` avale un etat sonde qui correspond a l'un d'eux, et solde avec lui tous les plus anciens : la liste du Project est en retard, donc un sondage peut montrer le premier de deux changements Discord rapides apres que le second a ete fait, et ne doit pas le defaire.

Avec la regle « aucun champ suivi n'a change, donc aucune ecriture » de `on_thread_update`, une action de l'utilisateur produit exactement une ecriture GitHub et une notification.

## Etat en memoire

Tout est porte par `HorizonBot` et reconstruit depuis GitHub au demarrage.

| Attribut | Type | Contient |
| --- | --- | --- |
| `known_github_items` | `set[str]` | Les cles vues au dernier sondage, pour detecter creations et suppressions |
| `known_github_details` | `dict[str, dict]` | Tous les champs de chaque tache tels que lus en dernier, pour detecter les changements |
| `known_github_sources` | `dict[str, str]` | Cle → URL du post Discord |
| `known_github_comments` | `dict[str, dict]` | Cle → id de commentaire → (corps, url) des commentaires deja vus |
| `relayed_comments` | `dict[str, int]` | Id de commentaire GitHub → le message Discord ou il a ete copie |
| `threads_deleted_by_bot` | `set[int]` | Les ids des posts que le bot supprime |
| `messages_deleted_by_bot` | `set[int]` | Les ids des messages que le bot supprime |
| `threads_edited_by_bot` | `dict[int, tuple]` | Id de post → la signature que le bot ecrit |
| `github_writes_from_discord` | `dict[str, list]` | Cle → les etats de tache ecrits par le bot et pas encore revus |
| `assignable_users` | `tuple` | Quand les logins assignables ont ete lus, et ces logins |

`forget_github_item()` retire une cle des quatre tables `known_github_*` et des echos en attente : c'est la seule facon correcte d'oublier une tache.

## Notifications

`notifications.py` decrit une card sans importer Discord :

```python
Notification(
    author="Task created • Discord → GitHub",
    title="🟢 Fix login",
    color=0x57F287,
    url="https://github.com/org/repo/issues/42",   # rend le titre cliquable
    fields=[("📈 Status", "Ready", True), …],      # (nom, valeur, inline)
)
```

`build_notification()` l'assemble, en ecartant les details et liens vides — l'API Discord refuse un champ a valeur vide — et en tronquant aux limites de l'API (256 pour un titre, 1024 pour la valeur d'un champ). `notification_embed()` dans `main.py` en fait un `discord.Embed`, et `notification_text()` rend la meme card en texte brut.

Une date s'ecrit comme un timestamp Discord (`discord_date()`, `<t:…:D>`), pour que chaque lecteur la voie dans son propre format de date. Elle est fixee a midi UTC, ce qui garde le meme jour calendaire dans tous les fuseaux d'une equipe.

`post_card()` ecrit le message d'ouverture d'un post du bot, un champ par ligne et sans icone : statut, priorite, taille, assignes, les dates s'il y en a, la description, et le lien GitHub. Elle reste sous les 2000 caracteres d'un message en raccourcissant la description.

### Les langues

La commande, sa syntaxe et les dix codes disponibles sont documentes dans le [README racine](../README.md#card-language). Ce qui suit est son cablage.

Tout ce qui traverse ce module est designe par les cles internes de `STATE_KEYS` — `title`, `status`, `priority`, `assignee`, `size`, `start_date`, `target_date` — jamais par un libelle que quelqu'un lit. `state_changes()` rend des triplets `(cle, avant, apres)` et `task_details()` un dictionnaire aux memes cles ; `build_notification()` et `post_card()` sont les endroits ou une cle devient un mot, via `language_words(language)`. Le formulaire lit ses libelles dans le meme fichier, via `HorizonBot.words`.

Ajouter une langue revient a ajouter un objet a `languages.json` avec les memes cles que les autres, ce que `test_notifications.py` verifie. Un code inconnu ou absent retombe sur l'anglais plutot que de lever une erreur, pour qu'une mauvaise valeur dans `.env` ne puisse pas bloquer une notification.

`HorizonBot.language` porte le code en cours. Il est lu au demarrage dans `.horizon-language`, avec `NOTIFICATION_LANGUAGE` en secours, et reecrit par `store_language()` quand la commande `!lang` en change. Un fichier qui ne peut pas etre ecrit est journalise : la langue s'applique quand meme pour ce lancement. La carte du formulaire est redessinee dans la nouvelle langue quand `!lang` la change.

La commande elle-meme est dans `handle_language_command()`, atteinte depuis `on_message` pour tout message du canal de notification. Elle exige la permission Gerer le serveur, refuse un code inconnu, et confirme par une card d'exemple construite dans la nouvelle langue.

`HorizonBot.notify()` sort immediatement si aucun canal de notification n'est configure, envoie l'embed, et bascule sur la version texte en cas de `discord.Forbidden` — le cas ou le bot n'a pas *Integrer des liens*. Tout autre echec est journalise et absorbe : la synchronisation est deja faite quand une card part, elle ne doit pas echouer a cause d'elle.

Ajouter un champ a une card ne touche que `build_notification()`. Ajouter un champ suivi touche `task_state()` et `STATE_KEYS`, qui doivent rester dans le meme ordre, et demande d'ajouter son libelle a chaque langue de `languages.json`.

## Operations GraphQL GitHub

Tout passe par `GitHubClient._query()`, qui poste sur `https://api.github.com/graphql` et leve si la reponse contient des `errors`. `list_items()` et `item_of_issue()` lisent le meme fragment `ItemFields`, pour qu'un item ait la meme forme d'ou qu'il vienne.

| Methode | Operations |
| --- | --- |
| `create_issue` | `createIssue`, puis label, assignes, `addProjectV2ItemById`, statut, priorite, taille, dates, `closeIssue` |
| `update_task` | N'ecrit que les champs donnes : `updateIssue`, label et champ de priorite, statut, taille, dates, assignes, `closeIssue` / `reopenIssue` |
| `update_issue_from_discord` / `update_linked_task` | `find_linked_item`, puis `update_task` |
| `update_optional_fields` | La taille et les dates : via `set_issue_field` si le depot a un champ d'issue de ce nom, sinon `updateProjectV2ItemFieldValue` ou `clearProjectV2ItemFieldValue` ; un champ que ni l'un ni l'autre n'a est saute |
| `issue_fields` / `set_issue_field` | `Repository.issueFields`, lu une fois ; `updateIssueFieldValue` pour en ecrire un, `deleteIssueFieldValue` pour l'effacer |
| `update_native_priority` | `set_issue_field` sur le champ d'issue de priorite ; saute avec un warning si le champ manque, comme sur un compte perso |
| `set_assignees` | `removeAssigneesFromAssignable` et `addAssigneesToAssignable`, pour que les assignes soient exactement les logins donnes |
| `comment_from_discord` | `find_linked_item`, puis `addComment` |
| `edit_comment_from_discord` / `delete_comment_from_discord` | Les commentaires de l'issue, puis `updateIssueComment` / `deleteIssueComment` sur celui copie depuis le message |
| `item_of_issue` | `resource(url:)` avec `Issue.projectItems`, l'item du Project sans attendre la liste |
| `add_issue_to_project` | `addProjectV2ItemById`, ou l'item existant si le workflow d'ajout automatique l'a ajoute avant |
| `add_label` | Lit `Repository.labels`, `createLabel` si absent, `addLabelsToLabelable` |
| `assignable_users` | `Repository.assignableUsers`, pour le formulaire |
| `project_metadata` / `project_field` | Les champs du Project, lus une fois et relus quand un champ ou une option manque |
| `set_discord_source` | `updateIssue` ou `updateProjectV2DraftIssue` |
| `delete_task_from_discord` | `deleteIssue`, avec repli sur `deleteProjectV2Item` |
| `list_items` | Tous les items du Project, 100 par page, avec contenu, champs et 100 derniers commentaires |

`create_project_item()` et `update_project_text()` sont des restes de l'approche par draft items et ne sont appeles par rien. Ils fonctionnent toujours, mais le chemin actif cree de vraies issues.

## Gestion des erreurs

La regle : une action deja faite ne doit pas etre annulee par l'echec de ce qui suit.

- Chaque handler enveloppe son travail dans un `try/except Exception` et journalise avec `logger.exception`, pour qu'un evenement fautif ne tue pas le client.
- La boucle de sondage attrape tout par iteration et continue ; une iteration ratee est retentee a la suivante, puisque `known_github_items` n'est mis a jour qu'apres un passage reussi.
- Un assigne qui ne peut pas etre pose n'empeche ni la creation de l'issue, ni son ajout au Project.
- Une notification qui ne part pas est journalisee, jamais relevee.
- Une action du formulaire qui echoue le dit dans le pied de la carte, qui garde ce qui avait ete rempli.

## Tests

| Fichier | Couvre | Besoin de |
| --- | --- | --- |
| `test_sync.py` | Lecture des posts et des items, corps d'issue, dates, extraction d'URL | rien |
| `test_notifications.py` | Contenu des cards, diff des champs, fiches de post, dates, limites de l'API, repli texte | rien |
| `test_github_client.py` | Pagination, ecriture des champs, assignes, recherche de commentaire, sur un faux `_query` | httpx |
| `test_form.py` | La carte du formulaire : sa disposition, le calendrier, ses fenetres, l'enregistrement | discord.py |
| `test_bot.py` | Synchronisation dans les deux sens, echos, fermeture, commentaires, formulaire, sur des faux | discord.py |

```bash
.venv/Scripts/python -m unittest tests.test_sync tests.test_notifications tests.test_github_client tests.test_form tests.test_bot
```

`test_bot.py` fait tourner le vrai `HorizonBot` contre un `FakeGitHub` qui garde une tache telle que la liste la rend, et un `FakeThread` dont les editions le modifient comme Discord les applique. `poll_once()` fait une iteration de sondage, et `KnownTaskTestCase` part d'un bot qui a deja pris sa reference. C'est le motif a reprendre pour tout nouveau comportement des handlers.

## Limites et pistes

- **Les labels (20), les assignes (10) et les commentaires (100) ne sont pas pagines** par issue. Au-dela de 100 commentaires, une suppression parmi les plus anciens n'est pas vue.
- **5000 items au plus** : `list_items()` s'arrete apres 50 pages et journalise un warning.
- **Discord limite les renommages de post**, a environ deux par post toutes les dix minutes. discord.py attend la fin de la limite dans `refresh_post()`, ce qui bloque la boucle de sondage pendant ce temps.
- **Un message se modifie et se supprime la ou il a ete ecrit.** Un commentaire venu de Discord modifie sur GitHub, ou une copie venue de GitHub modifiee sur Discord, ne revient pas de l'autre cote.
- **Les liens de pieces jointes Discord expirent** au bout d'environ un jour : les liens copies dans un commentaire GitHub cessent de marcher, le fichier lui-meme n'est pas televerse.
- **La reference du sondage** est prise au demarrage, donc les changements faits bot eteint ne sont jamais vus.
- **La liste des posts de la carte ne montre que les posts actifs**, ceux que Discord liste ; un post archive se retrouve en collant son lien.
- **Une seule carte pour toute l'equipe** : deux personnes qui l'utilisent en meme temps se partagent la meme tache.
