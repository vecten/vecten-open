variable "identifier" {
  description = "RDS instance identifier, <name prefix>-<key>; destroy looks instances up by it."
  type        = string
}

variable "environment" {
  description = "Environment name; prod instances get deletion protection."
  type        = string
}

variable "destroying" {
  description = "True while vecten destroy runs, lifting deletion protection."
  type        = bool
}

variable "instance_class" {
  description = "RDS instance class."
  type        = string
}

variable "allocated_storage" {
  description = "Initial storage in GB."
  type        = number
}

variable "max_allocated_storage" {
  description = "Storage auto-grow ceiling in GB."
  type        = number

  validation {
    condition     = var.max_allocated_storage > var.allocated_storage
    error_message = "max_allocated_storage must exceed allocated_storage so storage auto-grows."
  }
}

variable "private_subnet_ids" {
  description = "Subnets of a private instance."
  type        = list(string)
}

variable "public_subnet_ids" {
  description = "Subnets of an instance opened to public_cidrs."
  type        = list(string)
}

variable "security_group_id" {
  description = "Security group of the instance."
  type        = string
}

variable "public_cidrs" {
  description = "Non-empty only when tfvars opts into a public endpoint."
  type        = list(string)
}

variable "log_line_prefix" {
  description = "log_line_prefix for the parameter group; null keeps the RDS default."
  type        = string
  default     = null
}
