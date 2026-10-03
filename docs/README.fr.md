# Documentation du code

Comment le bot est construit, pour qui doit le modifier. Pour ce qu'il fait et comment le lancer, voir le [README racine](../README.md). An English version is available: [README.en.md](README.en.md).

## Sommaire

1. [Carte des modules](#carte-des-modules)
2. [Demarrage](#demarrage)
3. [La tache, vue par le bot](#la-tache-vue-par-le-bot)
4. [Discord → GitHub](#discord--github)
5. [GitHub → Discord](#github--discord-1)
6. [Le lien entre un post et une tache](#le-lien-entre-un-post-et-une-tache)
7. [Boucles et echos](#boucles-et-echos)
8. [Etat en memoire](#etat-en-memoire)
9. [Notifications](#notifications)
10. [Operations GraphQL GitHub](#operations-graphql-github)
11. [Gestion des erreurs](#gestion-des-erreurs)
12. [Tests](#tests)
13. [Limites et pistes](#limites-et-pistes)

---

## Carte des modules

| Module | Role | Importe Discord ? |
| --- | --- | --- |
| [`config.py`](../bot_horizon/config.py) | `Settings`, dataclass figee construite depuis l'environnement | non |
| [`sync.py`](../bot_horizon/sync.py) | Lecture et mise en forme pures : lire un post, lire un item du Project, extraire un id | non |
| [`notifications.py`](../bot_horizon/notifications.py) | Construit le contenu d'une card de notification, dans n'importe quelle langue | non |
| [`languages.json`](../bot_horizon/languages.json) | Les mots des cards, un objet par langue | non |
| [`github_client.py`](../bot_horizon/github_client.py) | Client GraphQL, une methode par operation | non |
| [`main.py`](../bot_horizon/main.py) | `HorizonBot` : handlers d'evenements, boucle de sondage, etat en memoire | oui |

Ce decoupage compte : tout ce qui est hors de `main.py` se teste sans connexion Discord, et `sync.py` comme `notifications.py` ne font aucune entree-sortie. Une nouvelle logique de lecture ou de mise en forme va la, pas dans un handler.

## Demarrage

`run()` dans `main.py` charge le `.env`, construit `Settings`, instancie `GitHubClient` puis `HorizonBot`, et se connecte. `on_ready` demarre la boucle de sondage en tache de fond, une seule fois : une reconnexion Discord redeclenche `on_ready`, et le garde `if self.poll_task is None` evite une deuxieme boucle.

`Settings.from_environment()` echoue tot : sans `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `GITHUB_TOKEN` ou `GITHUB_PROJECT_ID`, l'erreur arrive avant la connexion. Toutes les autres variables ont une valeur par defaut, donc le bot tourne avec ces quatre-la.

## La tache, vue par le bot

Les deux cotes sont ramenes au meme dictionnaire, par `discord_task_data()` pour un post et `github_item_data()` pour un item du Project :

```python
{
    "title": "Fix login",
    "status": "Backlog",        # l'un de STATUS_NAMES
    "priority": "Low",          # l'un de PRIORITY_NAMES
    "assignees": ["alice"],     # logins GitHub
    "assignee": "alice",        # les memes, joints pour l'affichage
    "description": "…",
}
```

`task_state()` le reduit aux quatre champs suivis — titre, statut, priorite, assignees — sous forme de tuple. Comparer deux tuples est la facon dont le bot decide qu'il s'est passe quelque chose, et `state_changes()` transforme une difference en triplets `(cle, avant, apres)`, qu'une card rend dans sa propre langue. Ce qui n'est pas dans ce tuple est invisible a la detection de changement.

Lecture d'un post : le statut et la priorite viennent du **premier tag de forum correspondant**, avec `Backlog` et `Low` par defaut. Le titre est le nom du post. `Assigned to:` et `Description:` sont lus dans le message d'ouverture par des regex insensibles a la casse, et une liste d'assignees accepte `,`, `;`, `/` ou l'espace comme separateur, avec un `@` optionnel.

Lecture d'un item du Project : le statut vient du champ single-select `Status` ; la priorite d'un label d'issue portant un nom de priorite, sinon du champ de priorite configure ; les assignees des vrais assignees de l'issue, sinon du champ texte configure ou d'un champ nomme `Assigned to`.

## Discord → GitHub

Trois handlers, tous gardes par `is_configured_channel()`, qui accepte le salon forum lui-meme et n'importe lequel de ses posts (`parent_id`).

### `on_message` — creation

Un message du canal de notification est traite en premier, comme une commande, et ne devient jamais une tache. Tout autre message du forum passe par la creation : les messages de bot et le contenu vide sont ignores, puis `create_issue()` est appele, qui :

1. cree une vraie issue dans `GITHUB_REPOSITORY_ID` (`createIssue`) ;
2. ajoute un label de priorite, en le creant avec une couleur si le depot ne l'a pas ;
3. assigne les logins connus, en ignorant et journalisant les inconnus ;
4. ajoute l'issue au Project (`addProjectV2ItemById`) ;
5. renseigne le `Status` du Project et le champ de priorite du depot.

Le corps de l'issue est construit par `discord_message_body()` et commence par `GITHUB_MARKER` : c'est ainsi que le sondage reconnait plus tard une tache venue de Discord, a ne pas republier.

### `on_thread_update` — modification

Declenche quand un post est renomme, retague, archive, epingle ou passe en mode lent. Le handler :

1. ecarte l'echo d'une edition que le bot vient de faire (voir [Boucles et echos](#boucles-et-echos)) ;
2. ignore les posts dont le message d'ouverture appartient au bot ;
3. compare `task_state(before)` et `task_state(after)` et **sort si aucun champ suivi n'a change** — c'est ce qui empeche un archivage ou une epingle d'ecrire sur GitHub ;
4. appelle `update_issue_from_discord()`, qui met a jour le titre, remplace le label de priorite, renseigne le statut du Project et le champ de priorite natif, et rend la cle de l'item ;
5. enregistre cette ecriture dans `github_writes_from_discord`, puis notifie.

A noter : les assignees ne sont pas reecrits ici. Apres la creation, seuls le titre, le statut et la priorite circulent dans ce sens.

### `on_thread_delete` — suppression

Ignore les suppressions faites par le bot lui-meme, puis `delete_task_from_discord()` retrouve l'item lie et supprime l'issue, en se rabattant sur le retrait de l'item du Project si l'issue ne peut pas etre supprimee. Un draft item ne voit que son item de Project supprime. La cle est ensuite oubliee localement pour que le sondage suivant n'y voie pas une suppression cote GitHub.

## GitHub → Discord

`poll_github()` tourne indefiniment, toutes les `GITHUB_POLL_INTERVAL_SECONDS`. Une iteration :

```
list_items()  →  {cle: item}       cle = url de l'issue, ou id de l'item pour un draft
│
├─ premier tour (known_github_items vide)
│     reconstruire les liens manquants, enregistrer chaque etat comme reference,
│     n'annoncer rien
│
├─ cle jamais vue
│     enregistrer son etat
│     └─ pas de GITHUB_MARKER et pas de ligne Source → publier un post,
│        reecrire le lien sur GitHub, annoncer une creation
│
├─ cle connue, etat different
│     ├─ identique a ce qu'on vient d'ecrire depuis Discord → avaler l'echo
│     └─ sinon → editer le post, annoncer une modification
│
└─ cle absente de la liste
      supprimer le post, oublier la cle, annoncer une suppression
```

Le premier tour n'annonce rien volontairement : il prend l'etat GitHub courant comme reference. Sans cela, toutes les taches existantes seraient republiees dans Discord a chaque redemarrage.

`publish_to_discord()` exige au moins un tag de forum correspondant au statut ou a la priorite ; sans aucun, il leve une erreur, qui est journalisee, et la tache est retentee au sondage suivant.

## Le lien entre un post et une tache

Le lien durable est la ligne `Source:` du corps de la tache GitHub, qui contient l'URL du post Discord. `discord_thread_id()` en extrait l'id du post.

- Une tache creee depuis Discord la porte des le depart, puisqu'elle fait partie du corps.
- Une tache creee sur le Project la recoit juste apres sa publication : `set_discord_source()` reecrit le corps de l'issue (`updateIssue`) ou du draft (`updateProjectV2DraftIssue`).

Comme il vit dans GitHub, ce lien survit a un redemarrage. Trois secours couvrent les cas ou il n'existe pas encore :

1. **`reconcile_links()` au demarrage** — pour chaque tache sans ligne `Source:`, il cherche parmi les posts du forum (actifs *et* archives) celui dont le nom correspond exactement au titre de la tache. Le bot nommant les posts d'apres le titre et maintenant les deux synchronises, une correspondance exacte identifie la bonne paire. Deux posts de meme nom laissent la tache sans lien, avec une ligne de log, plutot que d'en deviner un.
2. **`known_github_sources`** — la table en memoire, qui couvre la fenetre entre la connexion et la fin du premier sondage.
3. **Le nom du post** — `find_linked_item()` accepte un titre en dernier recours, et ne s'y fie que si une seule tache correspond.

## Boucles et echos

Chaque cote ecrit chez l'autre, donc chaque ecriture peut revenir sous forme d'evenement. Quatre gardes, chacun pour un echo precis :

| Garde | Echo qu'il supprime |
| --- | --- |
| `is_bot_message()` | Les messages du bot et les posts qu'il a ouverts |
| `threads_deleted_by_bot` | Le `on_thread_delete` d'un post que le bot vient de supprimer |
| `threads_edited_by_bot` | Le `on_thread_update` d'un post que le bot vient de renommer ou retaguer |
| `github_writes_from_discord` | Le changement de Project que le bot vient de pousser, revenu au sondage suivant |

Les deux derniers fonctionnent pareil : avant d'ecrire, le bot retient **ce qu'il s'apprete a ecrire**, et n'ecarte l'evenement entrant que s'il correspond exactement.

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

Avec la regle « aucun champ suivi n'a change, donc aucune ecriture » de `on_thread_update`, une action de l'utilisateur produit exactement une ecriture GitHub et une notification.

## Etat en memoire

Tout est porte par `HorizonBot` et reconstruit depuis GitHub au demarrage.

| Attribut | Type | Contient |
| --- | --- | --- |
| `known_github_items` | `set[str]` | Les cles vues au dernier sondage, pour detecter creations et suppressions |
| `known_github_states` | `dict[str, tuple]` | Le dernier etat connu de chaque tache, pour detecter les changements |
| `known_github_sources` | `dict[str, str]` | Cle → URL du post Discord |
| `threads_deleted_by_bot` | `set[int]` | Les ids des posts que le bot supprime |
| `threads_edited_by_bot` | `dict[int, tuple]` | Id de post → la signature que le bot ecrit |
| `github_writes_from_discord` | `dict[str, tuple]` | Cle → le (titre, statut, priorite) que le bot vient de pousser |

`forget_github_item()` retire une cle des trois premieres et de l'echo en attente : c'est la seule facon correcte d'oublier une tache.

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

### Les langues

La commande, sa syntaxe et les dix codes disponibles sont documentes dans le [README racine](../README.md#card-language). Ce qui suit est son cablage.

Tout ce qui traverse ce module est designe par les cles internes de `STATE_KEYS` — `title`, `status`, `priority`, `assignee` — jamais par un libelle que quelqu'un lit. `state_changes()` rend des triplets `(cle, avant, apres)` et `task_details()` un dictionnaire aux memes cles ; `build_notification()` est le seul endroit ou une cle devient un mot, via `language_words(language)`.

Ajouter une langue revient a ajouter un objet a `languages.json` avec les memes cles que les autres, ce que `test_notifications.py` verifie. Un code inconnu ou absent retombe sur l'anglais plutot que de lever une erreur, pour qu'une mauvaise valeur dans `.env` ne puisse pas bloquer une notification.

`HorizonBot.language` porte le code en cours. Il est lu au demarrage dans `.horizon-language`, avec `NOTIFICATION_LANGUAGE` en secours, et reecrit par `store_language()` quand la commande `!lang` en change. Un fichier qui ne peut pas etre ecrit est journalise : la langue s'applique quand meme pour ce lancement.

La commande elle-meme est dans `handle_language_command()`, atteinte depuis `on_message` pour tout message du canal de notification. Elle exige la permission Gerer le serveur, refuse un code inconnu, et confirme par une card d'exemple construite dans la nouvelle langue.

`HorizonBot.notify()` sort immediatement si aucun canal de notification n'est configure, envoie l'embed, et bascule sur la version texte en cas de `discord.Forbidden` — le cas ou le bot n'a pas *Integrer des liens*. Tout autre echec est journalise et absorbe : la synchronisation est deja faite quand une card part, elle ne doit pas echouer a cause d'elle.

Ajouter un champ a une card ne touche que `build_notification()`. Ajouter un champ suivi touche `task_state()` et `STATE_KEYS`, qui doivent rester dans le meme ordre, et demande d'ajouter son libelle a chaque langue de `languages.json`.

## Operations GraphQL GitHub

Tout passe par `GitHubClient._query()`, qui poste sur `https://api.github.com/graphql` et leve si la reponse contient des `errors`.

| Methode | Operations |
| --- | --- |
| `create_issue` | `createIssue`, puis label, assignees, `addProjectV2ItemById`, statut, priorite |
| `update_issue_from_discord` | `updateIssue`, `removeLabelsFromLabelable`, `addLabelsToLabelable`, statut, priorite |
| `update_native_priority` | Lit `Repository.issueFields`, ecrit `updateIssueFieldValue` |
| `add_label` | Lit `Repository.labels`, `createLabel` si absent, `addLabelsToLabelable` |
| `assign_issue` | Une requete `user(login:)` par login, puis `addAssigneesToAssignable` |
| `update_project_select` / `update_project_status` | `updateProjectV2ItemFieldValue` avec l'id de l'option |
| `set_discord_source` | `updateIssue` ou `updateProjectV2DraftIssue` |
| `delete_task_from_discord` | `deleteIssue`, avec repli sur `deleteProjectV2Item` |
| `list_items` | Lit les 100 premiers items du Project avec leur contenu et leurs champs |

`create_project_item()` et `update_project_text()` sont des restes de l'approche par draft items et ne sont appeles par rien. Ils fonctionnent toujours, mais le chemin actif cree de vraies issues.

## Gestion des erreurs

La regle : une action deja faite ne doit pas etre annulee par l'echec de ce qui suit.

- Chaque handler enveloppe son travail dans un `try/except Exception` et journalise avec `logger.exception`, pour qu'un evenement fautif ne tue pas le client.
- La boucle de sondage attrape tout par iteration et continue ; une iteration ratee est retentee a la suivante, puisque `known_github_items` n'est mis a jour qu'apres un passage reussi.
- Un assignee qui ne peut pas etre pose n'empeche ni la creation de l'issue, ni son ajout au Project.
- Une notification qui ne part pas est journalisee, jamais relevee.

## Tests

| Fichier | Couvre | Besoin de discord.py |
| --- | --- | --- |
| `test_sync.py` | Lecture des posts et des items, listes d'assignees, extraction d'URL | non |
| `test_notifications.py` | Contenu des cards, diff des champs, limites de l'API, repli texte | non |
| `test_bot.py` | Prevention des echos, de bout en bout, sur un faux client GitHub | oui |

```bash
.venv/Scripts/python -m unittest tests.test_sync tests.test_notifications tests.test_bot
```

`test_bot.py` fait tourner le vrai `HorizonBot` contre un `FakeGitHub`, en remplacant `notify` pour enregistrer les appels et en forcant une seule iteration de sondage avec `is_closed` qui rend `False` puis `True`. C'est le motif a reprendre pour tout nouveau comportement des handlers.

## Limites et pistes

- **`list_items` n'est pas pagine** (`items(first: 100)`), ni les labels (20) ni les assignees (10). Au-dela de 100 items, il faut une pagination par curseur.
- **Pas de handler `on_message_edit`** : modifier le corps d'un post, y compris `Assigned to:`, n'atteint jamais GitHub.
- **`on_message` se declenche sur chaque message du forum**, y compris les reponses dans un post existant, chacune traitee comme une nouvelle tache. Ne reagir qu'au message d'ouverture (`message.id == message.channel.id`) limiterait la creation aux nouveaux posts.
- **Les assignees ne sont pas reecrits** par `update_issue_from_discord`, qui n'envoie que titre, statut et priorite.
- **La reference du sondage** est prise au demarrage, donc les changements faits bot eteint ne sont jamais vus.
