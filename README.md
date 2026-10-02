# Horizon Discord / GitHub bot

Ce bot synchronise un canal Discord avec un GitHub Project :

- un message du canal cible est converti en tache GitHub ;
- une nouvelle tache du Project est publiee dans le canal Discord ;
- les messages generes par le bot sont ignores pour eviter les boucles.

## Format des taches

Dans le forum Discord, creer ces tags avec exactement ces noms :

- un tag de statut : `Backlog`, `Ready`, `In progress`, `In review` ou `Done` ;
- un tag de priorite : `Low`, `Medium`, `High` ou `Urgent`.

Un post peut contenir ce format :

```text
Assigned to: github-login
Description:
Description de la tache
```

Le bot utilise le premier tag de statut et le premier tag de priorite pour remplir les champs `Status` et `Priority` du Project. Le login dans `Assigned to` est ecrit dans un champ texte `Assignee` du Project. Cette approche cree un Draft item directement dans le Project, sans creer d'issue dans le depot.

Dans l'autre sens, un nouveau Draft item du Project est publie comme post forum avec la description, l'assignee, le tag de statut et le tag de priorite correspondants. Les items crees depuis Discord ne sont pas republies dans Discord.

Le Project doit contenir :

- un champ single-select `Status` avec les options `Backlog`, `Ready`, `In progress`, `In review`, `Done` ;
- un champ single-select `Task Priority` avec les options `Low`, `Medium`, `High`, `Urgent` ;
- un champ texte `Owner` (`Assignee` est un nom reserve par GitHub).

## Configuration

1. Creer un bot dans le [Discord Developer Portal](https://discord.com/developers/applications).
2. Activer **Message Content Intent** et inviter le bot avec les permissions `View Channel`, `Send Messages` et `Read Message History`.
3. Creer un token GitHub avec acces en lecture/ecriture au Project cible.
4. Copier `.env.example` vers `.env` et renseigner les valeurs.

Le Project ID est l'identifiant global `PVT_...`. Le token GitHub doit pouvoir lire et modifier le Project. Le dépôt GitHub n'est pas utilisé pour les Draft items.

## Lancer localement

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py -m bot_horizon.main
```

Le bot doit etre lance sur une machine ou un serveur qui reste actif. Pour la production, utiliser un service systemd, Docker ou une pipeline de deploiement.
