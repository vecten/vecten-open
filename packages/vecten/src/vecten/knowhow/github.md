# Setting up GitHub

Your platform's project lives on GitHub: every connector, data model and metric
definition is kept there, with the full history of who changed what and why. GitHub
also runs the deploy. When a change is approved and merged, GitHub updates the
platform in your cloud, so nobody needs to deploy from their own computer.

## Step 1: Create an account

If you do not have a GitHub account yet, go to github.com and choose **Sign up**. A
free account is enough. If your company already has a GitHub organisation, ask whoever
manages it to add you, so the project can live there rather than under your personal
name.

## Step 2: Install the GitHub command-line tool

I talk to GitHub through its command-line tool, called `gh`. On a Mac with Homebrew,
install it with `brew install gh`; for other systems, follow the instructions at
cli.github.com. I can run the install for you.

## Step 3: Log in

Run:

```
gh auth login
```

Choose **GitHub.com**, then **HTTPS**, then **Login with a web browser**. The tool
shows a short code and opens a browser window; enter the code there and approve. From
then on, `gh` acts as you when I create the repository and push changes, and you can
sign out at any time with `gh auth logout`.

## Step 4: Choose a repository name

A repository is the project's home on GitHub. Its name has two parts, the owner and
the name, written as `owner/name`. The owner is your user name or your organisation's
name; the name is up to you. For example, `acme/data-platform` puts a repository
called `data-platform` under the `acme` organisation. Tell me the name you want, and
whether it should be under you or your organisation. You do not need to create it
yourself.

## What happens next

When the time comes, I run `vecten github init`. It creates the repository under the
name you chose and uploads the project to it.

Later, `vecten bootstrap` sets up a trust between your GitHub repository and your
cloud account. This trust, called OIDC, lets GitHub prove to your cloud that a deploy
really comes from your repository, and the cloud lets it in for that one job only.
Because of it, no password or access key is ever copied into GitHub, pasted into a
settings page, or written into a file. There is nothing to leak and nothing to rotate,
and if you ever want to cut GitHub off, you remove the trust in your cloud account and
it stops working at once.
