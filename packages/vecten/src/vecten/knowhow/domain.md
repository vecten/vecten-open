# Setting up a domain

Your platform needs a web address so that you and your colleagues can reach it from
anywhere. That address lives under a domain you own, such as `example.com`. Under it,
Vecten Open creates two addresses:

- `airflow.<domain>`, where you can watch your data being loaded on schedule;
- `mcp.<domain>`, the address people connect their AI assistants to, to ask questions
  of your data.

So if your domain is `example.com`, the platform appears at `airflow.example.com` and
`mcp.example.com`. Your existing website and email are not affected. If you prefer to
keep the platform apart from your main domain, you can use a part of it, such as
`data.example.com`, and the addresses become `airflow.data.example.com` and
`mcp.data.example.com`.

## Step 1: Buy a domain, if you do not have one

Any registrar will do: the company you buy a domain name from. Well-known ones include
Namecheap, Gandi, GoDaddy and OVH, and both AWS and Scaleway sell domains too. Search
for a name, pay for it, and you own it for as long as you renew it, usually yearly. If
your company already owns a domain, ask whoever manages it; you will need their help
for step 3.

## Step 2: Create a zone in your cloud

A zone is the list of addresses under your domain and where each one points. The cloud
needs to manage this list, so that it can add the two addresses above when the
platform is created and keep them pointing to the right place.

- On **AWS**, open **Route 53** in the console, choose **Hosted zones**, then
  **Create hosted zone**. Enter your domain, or the part of it you want to use, and
  keep the type **Public hosted zone**.
- On **Scaleway**, open **Domains and DNS** in the console and choose **Add an external
  domain** (or **Create a DNS zone** for a part of a domain you already manage there).
  Enter the name.

When the zone is created, the cloud shows a short list of name servers: addresses
that look like `ns-123.awsdns-45.com` on AWS or `ns0.dom.scw.cloud` on Scaleway.
Keep this list at hand. I can also read it for you once the zone exists.

## Step 3: Point your domain to the cloud

This step is called delegation. It tells the rest of the internet that your cloud,
not your registrar, now answers for the zone.

- **If the zone is your whole domain**, sign in to your registrar, find the name server
  settings for the domain, choose to use custom name servers, and replace the existing
  ones with the list from step 2.
- **If the zone is a part of your domain**, such as `data.example.com`, leave the
  domain's name servers alone. Instead, wherever your domain's records are managed,
  add `NS` records for `data` that list the name servers from step 2.

Changes can take from a few minutes to a day to spread across the internet.

## Step 4: Check that it worked

I check this for you by running:

```
dig NS <zone>
```

with your zone in place of `<zone>`, for example `dig NS data.example.com`. When the
answer lists the same name servers the cloud gave you in step 2, the delegation is
done and I can create the platform's addresses. If it still shows the old ones, we
wait a little and try again.
