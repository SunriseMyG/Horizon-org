# Horizon Discord / GitHub bot

Ce bot synchronise un canal Discord avec un GitHub Project :

- un message du canal cible est converti en tache GitHub ;
- une nouvelle tache du Project est publiee dans le canal Discord ;
- la suppression d'un post du forum supprime la tache GitHub, et inversement ;
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

## Suppression des taches

La suppression est synchronisee dans les deux sens :

- supprimer le post du forum Discord supprime l'issue GitHub liee (et donc son item dans le Project). Si l'issue ne peut pas etre supprimee, l'item est au moins retire du Project. Pour un Draft item, seul l'item du Project est supprime ;
- supprimer une tache du Project (ou retirer l'item du Project) supprime le post du forum Discord correspondant au prochain sondage.

Chaque cote ignore les suppressions declenchees par le bot lui-meme pour eviter une boucle.

### Le lien entre les deux cotes

Le lien est la ligne `Source:` du corps de la tache GitHub, qui contient l'URL du post Discord.

- une tache creee depuis Discord la contient des sa creation ;
- une tache creee sur le Project la recoit juste apres sa publication dans Discord : le bot reecrit le corps de l'issue (`updateIssue`) ou du Draft item (`updateProjectV2DraftIssue`) pour y ajouter la ligne.

Le lien est donc stocke dans GitHub et survit a un redemarrage du bot.

Aucune action manuelle n'est necessaire. Au demarrage, le bot reconstruit les liens manquants : pour chaque tache sans ligne `Source:`, il cherche parmi les posts du forum (actifs et archives) celui dont le nom correspond exactement au titre de la tache, et ecrit le lien. Le bot nommant les posts d'apres le titre de la tache et maintenant les deux synchronises, cette correspondance identifie la bonne paire. Si deux posts portent le meme titre, la tache est laissee sans lien plutot que d'en deviner un, et le bot le signale dans ses logs.

En secours, le bot utilise aussi son suivi en memoire et le nom du post supprime, ce qui couvre la fenetre entre sa connexion et la fin du premier sondage.

Le Project doit contenir :

- un champ single-select `Status` avec les options `Backlog`, `Ready`, `In progress`, `In review`, `Done` ;
- un champ single-select `Task Priority` avec les options `Low`, `Medium`, `High`, `Urgent` ;
- un champ texte `Owner` (`Assignee` est un nom reserve par GitHub).

## Configuration

1. Creer un bot dans le [Discord Developer Portal](https://discord.com/developers/applications).
2. Activer **Message Content Intent** et inviter le bot avec les permissions `View Channel`, `Send Messages`, `Read Message History`, `Create Posts` et `Manage Threads` (necessaire pour supprimer un post du forum).
3. Creer un token GitHub avec acces en lecture/ecriture au Project cible.
4. Copier `.env.example` vers `.env` et renseigner les valeurs.

Le Project ID est l'identifiant global `PVT_...`. Le token GitHub doit pouvoir lire et modifier le Project. Le dépôt GitHub n'est pas utilisé pour les Draft items.

## Lancer localement

Les scripts `run.ps1` (Windows) et `run.sh` (Linux, macOS, Git Bash) font tout :
ils creent `.venv` si besoin, installent `requirements.txt`, verifient `.env`
puis demarrent le bot.

```powershell
.\run.ps1
```

```bash
./run.sh
```

Au premier lancement, le script cree `.env` depuis `.env.example` et s'arrete :
remplir au minimum `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `GITHUB_TOKEN` et
`GITHUB_PROJECT_ID`, puis relancer. Les dependances ne sont reinstallees que si
`requirements.txt` a change. L'option `-NoInstall` / `--no-install` saute
completement cette etape.

Le script cherche un interpreteur Python 3.10 ou plus parmi `python`, `py` et
`python3`. Les installations du Microsoft Store ne fournissent pas le launcher
`py`, celles de python.org si.

### Sans script

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m bot_horizon.main
```

Le bot doit etre lance sur une machine ou un serveur qui reste actif. Pour la production, utiliser un service systemd, Docker ou une pipeline de deploiement.
