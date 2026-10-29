# Get Emet

The plugin is free and open source. It sends your turns to Emet, which needs an Emet account for your organization. Without one there is no key and no Team ID to set, so the plugin has nowhere to send.

## Talk to us

[Contact Emet](https://emet.so/contact?topic=pricing) to buy Emet or book a demo. Tell us:

- how many developers will use the plugin, and whether you plan to roll it out to everyone through managed settings ([docs/enterprise.md](enterprise.md));
- which capture mode you expect to use, and whether a security review comes first;
- the agents and tools you run besides Claude Code.

We reply within one business day.

## After you buy

Once your organization is set up in Emet:

1. Under **Teams**, copy your team's **External ID**: the Team ID the plugin asks for.
2. Under **Applications**, then **Manage keys**, mint a key for the team.
3. Invite each developer, so their turns are attributed to them. Use the email they sign in to Claude with. A developer without a Claude sign-in (an API key, Bedrock, Vertex AI or a gateway), or whose Claude email differs, sets **Your Emet email** (`emet_email`) to the address you invited ([how](enterprise.md#4-who-ran-each-turn)).

Then [install the plugin](../README.md#install-with-your-agent), or have an administrator [roll it out to the organization](enterprise.md).

## Already a customer

For help with the plugin or a rollout, open an [issue](https://github.com/FUSEis/emet-plugin/issues) or write to support@emet.so. Report vulnerabilities as described in [SECURITY.md](../SECURITY.md).
