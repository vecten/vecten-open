variable "environment" {
  description = "Environment name, the tfvars file stem (prod, staging, ...)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]*(-[a-z0-9]+)*$", var.environment)) && length(var.environment) <= 32 && var.environment != "local"
    error_message = "environment is lowercase letters, digits and hyphens, starting with a letter, with no hyphen at the end or next to another, at most 32 characters, and not local."
  }
}

variable "project_name" {
  description = "Project name from vecten.toml; secret paths use it verbatim."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9_-]{0,62}$", var.project_name))
    error_message = "project_name starts with a lowercase letter and holds at most 63 of [a-z0-9_-], the rule vecten init applies."
  }
}

variable "domain" {
  description = "Domain under which airflow.<domain> and mcp.<domain> are issued."
  type        = string

  validation {
    condition     = var.domain == var.dns_zone || endswith(var.domain, ".${var.dns_zone}")
    error_message = "domain must be dns_zone or a name inside it."
  }
}

variable "dns_zone" {
  description = "Scaleway Domains and DNS zone that holds the records."
  type        = string
}

variable "region" {
  description = "Scaleway region of every resource (fr-par, nl-ams, pl-waw); zonal resources sit in <region>-1."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]{3}$", var.region))
    error_message = "region is a Scaleway region name such as fr-par, nl-ams or pl-waw."
  }
}

# Part of the inputs every cloud module shares; Scaleway has no OIDC trust to build from it.
# tflint-ignore: terraform_unused_declarations
variable "github_repository" {
  description = "owner/name of the repository whose Actions deploy this environment."
  type        = string
}

variable "components" {
  description = "Components to deploy: platform, mcp or both."
  type        = list(string)

  validation {
    condition = (
      length(var.components) > 0
      && length(var.components) == length(distinct(var.components))
      && alltrue([for c in var.components : contains(["platform", "mcp"], c)])
    )
    error_message = "components is a non-empty list of distinct entries from: platform, mcp."
  }
}

variable "profile" {
  description = "standard runs one VM per component and separate state and warehouse instances; minimal runs one VM and one instance."
  type        = string

  validation {
    condition     = contains(["standard", "minimal"], var.profile)
    error_message = "profile must be standard or minimal."
  }
}

variable "size" {
  description = "Sizing tier of the MCP VM and the warehouse instance."
  type        = string

  validation {
    condition     = contains(keys(jsondecode(file("${path.module}/sizing.json")).tiers), var.size)
    error_message = "size must be one of the tiers in sizing.json: small, medium, large."
  }
}

variable "warehouse" {
  description = "Warehouse kind; a Postgres warehouse gets its own database instance under standard."
  type        = string

  validation {
    condition     = contains(["postgres", "snowflake"], var.warehouse)
    error_message = "warehouse must be postgres or snowflake."
  }
}

# One tfvars file feeds both the cloud root and the Snowflake root.
# tflint-ignore: terraform_unused_declarations
variable "snowflake_account" {
  description = "Snowflake ORG-ACCOUNT, read by the Snowflake root; the Scaleway module ignores it."
  type        = string
  default     = null
}

variable "platform_size" {
  description = "Sizing tier of the platform VM, which scales with concurrent syncs rather than users."
  type        = string
  default     = "small"
  nullable    = false

  validation {
    condition     = contains(keys(jsondecode(file("${path.module}/sizing.json")).tiers), var.platform_size)
    error_message = "platform_size must be one of the tiers in sizing.json: small, medium, large."
  }
}

variable "root_volume_size_gb" {
  description = "Size in GB of every VM's boot volume, which holds the images and the named volumes. A DEV1 type boots from its local SSD, which caps the size at the type's local storage."
  type        = number
  default     = 30
  nullable    = false

  # An SBS volume can grow but never shrink, so a size below the one every VM was created
  # with would replace each VM.
  validation {
    condition     = var.root_volume_size_gb >= 30 && floor(var.root_volume_size_gb) == var.root_volume_size_gb
    error_message = "root_volume_size_gb is a whole number of GB, at least 30."
  }
}

variable "instance_type_overrides" {
  description = "Instance and node types that win over sizing.json, keyed by platform, mcp, main, state or warehouse."
  type        = map(string)
  default     = {}
  nullable    = false

  validation {
    condition     = alltrue([for k in keys(var.instance_type_overrides) : contains(["platform", "mcp", "main", "state", "warehouse"], k)])
    error_message = "instance_type_overrides keys must be among: platform, mcp, main, state, warehouse."
  }

  # User data installs the amd64 Compose and buildx builds.
  validation {
    condition = alltrue([
      for k, t in var.instance_type_overrides :
      !can(regex("^(AMP2|COPARM1|ARM64)-|^[A-Z]+[0-9]+-A[0-9]+C-", upper(t))) if contains(["platform", "mcp", "main"], k)
    ])
    error_message = "instance_type_overrides names an ARM server type (AMP2, COPARM1, ARM64, or an -A<n>C- type such as BASIC2-A2C-4G); vecten VMs need an x86_64 type such as DEV1, POP2, PRO2, PLAY2 or the -X<n>C- types."
  }
}

variable "database_public_cidrs" {
  description = "Explicit opt-in: CIDRs the instances' public endpoint admits. Empty keeps every instance on the private network only."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for c in var.database_public_cidrs : can(cidrhost(c, 0)) && !endswith(c, "/0")])
    error_message = "database_public_cidrs holds IPv4 CIDRs narrower than /0."
  }
}

variable "destroying" {
  description = "Set by vecten destroy before terraform destroy: lets the artifacts bucket be emptied."
  type        = bool
  default     = false
  nullable    = false
}

variable "project_id" {
  description = "ID of the environment's resources project, which bootstrap creates; never the state project."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.project_id))
    error_message = "project_id is a Scaleway project ID, a UUID."
  }
}

variable "dns_project_id" {
  description = "ID of the project holding dns_zone; null means the resources project."
  type        = string
  default     = null

  validation {
    condition     = var.dns_project_id == null || can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.dns_project_id))
    error_message = "dns_project_id is a Scaleway project ID, a UUID."
  }
}

variable "operator_principal" {
  description = "The bucket-policy principal bootstrap ran as, user_id:<id> or application_id:<id>, which has full access to the artifacts bucket; null grants no operator."
  type        = string
  default     = null

  validation {
    condition     = var.operator_principal == null || can(regex("^(user_id|application_id):[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.operator_principal))
    error_message = "operator_principal is user_id:<uuid> or application_id:<uuid>, as bootstrap records it."
  }
}

variable "application_ids" {
  description = "IAM application IDs bootstrap created, keyed by deploy, plan and each host key; the bucket policy grants them."
  type        = map(string)

  validation {
    condition = alltrue([
      for k in concat(["deploy", "plan"], var.profile == "minimal" ? ["main"] : var.components) :
      contains(keys(var.application_ids), k)
    ])
    error_message = "application_ids lacks an entry for ${join(", ", [for k in concat(["deploy", "plan"], var.profile == "minimal" ? ["main"] : var.components) : k if !contains(keys(var.application_ids), k)])}; run vecten bootstrap for this environment, which creates an application for deploy, plan and every host."
  }

  validation {
    condition     = alltrue([for id in values(var.application_ids) : can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", id))])
    error_message = "every application_ids value is a Scaleway IAM application ID, a UUID."
  }
}

variable "state_admin_password_version" {
  description = "Version of the state instance's write-only admin password; raising it sets a new password and its secret together."
  type        = number
  default     = 1
  nullable    = false

  validation {
    condition     = var.state_admin_password_version >= 1 && floor(var.state_admin_password_version) == var.state_admin_password_version
    error_message = "state_admin_password_version is a whole number of at least 1."
  }
}

variable "warehouse_admin_password_version" {
  description = "Version of the warehouse instance's write-only admin password; raising it sets a new password and its secret together."
  type        = number
  default     = 1
  nullable    = false

  validation {
    condition     = var.warehouse_admin_password_version >= 1 && floor(var.warehouse_admin_password_version) == var.warehouse_admin_password_version
    error_message = "warehouse_admin_password_version is a whole number of at least 1."
  }
}
