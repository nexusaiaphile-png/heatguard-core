# HeatGuard AI public website

This directory contains the static public marketing site. It is intentionally separate from the FastAPI backend so the public website can be deployed as a low-cost Render Static Site without changing the live API service.

## Deploy on Render

After this branch is reviewed and merged into `main`:

1. Create a **Static Site** in the existing Render workspace.
2. Connect `nexusaiaphile-png/heatguard-core`, branch `main`.
3. Set **Publish Directory** to `website` (no build command is required).
4. Keep auto-deploy enabled.
5. After the static site is live, add the purchased domain under the static site's Custom Domains and follow the exact DNS records Render provides.

## Before public launch

- Add a verified business contact email or enquiry endpoint to the contact section.
- Confirm final pricing, VAT treatment and distributor commission agreement.
- Review privacy policy, terms, and any claims about alerts or supported camera models.
- Keep the thermal dashboard graphic clearly labelled as an illustration; it is not live customer telemetry.
- Test mobile layout, accessibility, domain DNS and HTTPS.

The page does not connect to customer data, collect enquiries, or claim that end-to-end camera alerts have been verified. Those are deliberate safeguards until the corresponding features are implemented and tested.