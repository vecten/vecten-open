variable "name" {
  description = "Database instance name, <name prefix>-<key>; destroy finds instances by it."
  type        = string
}

variable "project_id" {
  description = "Resources project of the instance."
  type        = string
}

variable "region" {
  description = "Region of the instance."
  type        = string
}

variable "node_type" {
  description = "RDB node type, such as DB-PLAY2-PICO."
  type        = string
}

variable "volume_type" {
  description = "SBS volume type; lssd cannot take a fixed size."
  type        = string

  validation {
    condition     = contains(["sbs_5k", "sbs_15k"], var.volume_type)
    error_message = "volume_type must be sbs_5k or sbs_15k."
  }
}

variable "volume_size_gb" {
  description = "Volume size in GB; an SBS volume grows by an edit and never shrinks."
  type        = number
}

variable "private_network_id" {
  description = "Private network of the instance's only default endpoint."
  type        = string
}

variable "public_cidrs" {
  description = "Non-empty only when tfvars opts into a public endpoint; its ACL admits exactly these."
  type        = list(string)
}

variable "engine_settings" {
  description = "Engine settings, from the list Managed Databases offer for the engine version."
  type        = map(string)
}

variable "admin_password" {
  description = "Password of vecten_admin, taken only through the write-only argument."
  type        = string
  sensitive   = true
  ephemeral   = true
}

variable "admin_password_version" {
  description = "Version of admin_password; a new version sets the password again."
  type        = number
}

variable "tags" {
  description = "Tags on the instance."
  type        = list(string)
}
