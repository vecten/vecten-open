# Privacy

Vecten Open is software you run yourself. Vecten, the company that maintains it, receives nothing
from it.

- **No usage data.** The `vecten` CLI, the four Python packages and the two container images send
  no usage data or telemetry to Vecten or anyone else. They switch off dlt's and dbt's anonymous
  usage reporting, and the Apache Airflow version in the image has none.
- **The plugin.** The Claude Code plugin is a set of skill instructions. It contains no code of
  its own, collects nothing and calls no service.
- **Your data stays where you put it.** The platform and the MCP run on your laptop or in the cloud
  account you deploy them to, against your own warehouse. The MCP keeps its people, grants and
  audit records in your own database.
- **Network calls are the ones your setup asks for:** your cloud provider, your warehouse, the
  sources you connect, the package and image registries that install the software, and Let's
  Encrypt for the certificates of a deployment.
- **Your agent.** What an assistant such as Claude does with the answers it receives is covered
  by that assistant's provider and its privacy terms, not by this project.

Questions go to [Discussions](https://github.com/vecten/vecten-open/discussions); security reports
follow [SECURITY.md](SECURITY.md). Vecten's website and commercial services have their own
[privacy policy](https://vecten.com/privacy-policy).
