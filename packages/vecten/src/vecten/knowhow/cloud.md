# Setting up a cloud account

Your platform needs a cloud provider to run on once it leaves your computer. Vecten
Open supports two: AWS (Amazon Web Services) and Scaleway, a European provider. Pick
the one you prefer; I will ask which one when it matters. Below are the steps for
each. I will go through them with you, and you can stop and ask me at any point.

## AWS

### Create the account and enable billing

1. Go to aws.amazon.com and choose **Create an AWS Account**.
2. Enter an email address and an account name. Use a shared company address rather
   than a personal one, so the account does not depend on one person.
3. Add a payment card. AWS asks for it before you can create anything, because the
   servers and databases we create are billed monthly to this card.
4. Confirm your phone number and pick the free Basic support plan.
5. Sign in to the console with the email and password you chose.

Before anything is created, I can show you an estimate of the monthly cost.

### Install the command-line tool and log in

1. Install the AWS command-line tool, called `aws`. On a Mac with Homebrew, run
   `brew install awscli`; for other systems, follow the installer on the AWS website.
   I can run this for you.
2. In the AWS console, open **IAM Identity Center** and enable it. Enabling it also
   creates an AWS Organization around your account; that is expected. It gives you a
   sign-in that expires on its own, so no long-lived key sits on your computer. Then:
   - under **Users**, create a user for yourself. AWS emails that user an
     invitation: open it, set a password and sign in once as the new user;
   - under **Permission sets**, create one with the **AdministratorAccess** policy;
   - under **AWS accounts**, select your account and assign it that user and that
     permission set.
3. Run `aws configure sso`. It asks for the SSO start URL and the SSO region; both
   are on the **Settings** page of IAM Identity Center. A browser window opens; sign
   in there as the new user, not as the root account, and approve. When it asks for
   a profile name, answer `default`.
4. Confirm that it worked by running:

   ```
   aws sts get-caller-identity
   ```

   It prints your account number and user name. If it does, the setup is done. If you
   chose another profile name, run `aws sts get-caller-identity --profile <name>`
   instead; later commands then need `AWS_PROFILE=<name>` set, which I take care of.

## Scaleway

### Create the account and enable billing

1. Go to scaleway.com and choose **Sign up**.
2. Enter an email address and confirm it from the message Scaleway sends you. As with
   AWS, a shared company address is better than a personal one.
3. Fill in your company details and add a payment method under **Billing**. Scaleway
   needs it before it lets you create servers or databases.
4. Scaleway creates a default project for you. vecten leaves it alone: before the first
   deploy, it creates two projects of its own for each environment.

### Install the command-line tool and log in

1. Install the Scaleway command-line tool, called `scw`. On a Mac with Homebrew, run
   `brew install scw`; for other systems, follow the instructions on the Scaleway
   website. I can run this for you.
2. In the Scaleway console, open **IAM**, then **API keys**, and generate a key for
   yourself. The console shows a secret key once; keep that page open.
3. Run `scw init` and paste the access key and secret key when it asks. It stores them
   on your computer only, in Scaleway's own settings file, not in the project.
4. Confirm that it worked by running:

   ```
   scw account project list
   ```

   It lists your projects, including the default one. If it does, the setup is done.

## Region

A region is the city or area where the cloud keeps your servers and data. Unless you
tell me otherwise, I use Frankfurt for AWS and Paris for Scaleway, so your data stays
in the European Union:

- AWS: `eu-central-1`
- Scaleway: `fr-par`
