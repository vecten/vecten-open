variable "environment" {
  description = "Environment name, the tfvars file stem (prod, staging, ...)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]*(-[a-z0-9]+)*$", var.environment)) && length(var.environment) <= 32 && var.environment != "local"
    error_message = "environment is lowercase letters, digits and hyphens, starting with a letter, with no hyphen at the end or next to another, at most 32 characters, and not local."
  }
}

variable "project_name" {
  description = "Project name from vecten.toml; secret names use it verbatim."
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
  description = "Public Route 53 hosted zone that holds the records."
  type        = string
}

variable "region" {
  description = "AWS region of every resource."
  type        = string
}

# Part of the inputs every cloud module shares; on AWS bootstrap builds the OIDC trust from it.
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
  description = "Warehouse kind; a Postgres warehouse gets its own RDS instance under standard."
  type        = string

  validation {
    condition     = contains(["postgres", "snowflake"], var.warehouse)
    error_message = "warehouse must be postgres or snowflake."
  }
}

# One tfvars file feeds both the cloud root and the Snowflake root.
# tflint-ignore: terraform_unused_declarations
variable "snowflake_account" {
  description = "Snowflake ORG-ACCOUNT, read by the Snowflake root; the AWS module ignores it."
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
  description = "Size in GiB of every VM's root volume, which holds the images and the named volumes."
  type        = number
  default     = 30
  nullable    = false

  # EBS grows a volume in place but never shrinks one, so a size below the one every VM was
  # created with would fail the apply.
  validation {
    condition     = var.root_volume_size_gb >= 30 && var.root_volume_size_gb <= 16384 && floor(var.root_volume_size_gb) == var.root_volume_size_gb
    error_message = "root_volume_size_gb is a whole number of GiB from 30 to 16384, the gp3 maximum."
  }
}

variable "instance_type_overrides" {
  description = "Instance types that win over sizing.json, keyed by platform, mcp, main, state or warehouse."
  type        = map(string)
  default     = {}
  nullable    = false

  validation {
    condition     = alltrue([for k in keys(var.instance_type_overrides) : contains(["platform", "mcp", "main", "state", "warehouse"], k)])
    error_message = "instance_type_overrides keys must be among: platform, mcp, main, state, warehouse."
  }
}

variable "database_public_cidrs" {
  description = "Explicit opt-in: CIDRs allowed to reach the instances on 5432 from the internet. Empty keeps every instance private."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for c in var.database_public_cidrs : can(cidrhost(c, 0)) && !endswith(c, "/0")])
    error_message = "database_public_cidrs holds IPv4 CIDRs narrower than /0."
  }
}

variable "log_line_prefix" {
  description = "log_line_prefix on every instance holding the warehouse database; %u and %a name the session user and the impersonated person."
  type        = string
  default     = "%m:%r:%u@%d:[%p]:%l:%e:%s:%v:%x:%c:%q%a:"
  nullable    = false
}

variable "destroying" {
  description = "Set by vecten destroy before terraform destroy: lifts deletion protection and lets the artifacts bucket be emptied."
  type        = bool
  default     = false
  nullable    = false
}
