variable "environment" {
  description = "Environment name, the tfvars file stem (prod, staging, ...)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]*(-[a-z0-9]+)*$", var.environment)) && length(var.environment) <= 32 && var.environment != "local"
    error_message = "environment is lowercase letters, digits and hyphens, starting with a letter, with no hyphen at the end or next to another, at most 32 characters, and not local."
  }
}

variable "project_name" {
  description = "Project name from vecten.toml."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9_-]{0,62}$", var.project_name))
    error_message = "project_name starts with a lowercase letter and holds at most 63 of [a-z0-9_-], the rule vecten init applies."
  }
}

variable "size" {
  description = "Sizing tier; picks the warehouse size unless warehouse_size is set."
  type        = string

  validation {
    condition     = contains(keys(jsondecode(file("${path.module}/sizing.json"))), var.size)
    error_message = "size must be one of the tiers in sizing.json: small, medium, large."
  }
}

variable "components" {
  description = "Components deployed beside the warehouse; the platform builds it, the MCP adds PROVISIONER."
  type        = list(string)

  validation {
    condition = (
      contains(var.components, "platform")
      && length(var.components) == length(distinct(var.components))
      && alltrue([for c in var.components : contains(["platform", "mcp"], c)])
    )
    error_message = "components holds platform, and optionally mcp, each once: an MCP over an existing Snowflake account has no Snowflake module."
  }
}

variable "account" {
  description = "Snowflake account identifier ORGANIZATION-ACCOUNT; the provider authenticates against it from the SNOWFLAKE_* environment."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9]+-[A-Za-z0-9_]+$", var.account))
    error_message = "account is the ORGANIZATION-ACCOUNT identifier, for example ACME-PROD."
  }
}

variable "database_name" {
  description = "Database the convention's schemas live in."
  type        = string

  validation {
    condition     = can(regex("^[A-Z][A-Z0-9_]{0,254}$", var.database_name))
    error_message = "database_name is an unquoted upper-case Snowflake identifier."
  }
}

variable "warehouse_name" {
  description = "Warehouse every role computes on."
  type        = string

  validation {
    condition     = can(regex("^[A-Z][A-Z0-9_]{0,254}$", var.warehouse_name))
    error_message = "warehouse_name is an unquoted upper-case Snowflake identifier."
  }
}

variable "warehouse_size" {
  description = "Warehouse size; null takes the size tier's entry in sizing.json."
  type        = string
  default     = null

  validation {
    condition     = var.warehouse_size == null || contains(["XSMALL", "SMALL", "MEDIUM", "LARGE", "XLARGE"], coalesce(var.warehouse_size, "XSMALL"))
    error_message = "warehouse_size is one of XSMALL, SMALL, MEDIUM, LARGE, XLARGE."
  }
}

variable "loader_public_key" {
  description = "PEM public key of the LOADER service user's key pair; its private half never reaches Terraform."
  type        = string

  validation {
    condition     = strcontains(var.loader_public_key, "BEGIN PUBLIC KEY") && !strcontains(var.loader_public_key, "PRIVATE")
    error_message = "loader_public_key is a PEM public key; a private key never belongs in Terraform."
  }
}
