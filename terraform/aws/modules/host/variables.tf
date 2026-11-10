variable "name" {
  description = "Name of the instance, its role and its instance profile."
  type        = string
}

variable "host" {
  description = "Host key: platform, mcp or main."
  type        = string
}

variable "ami_id" {
  description = "Amazon Linux 2023 AMI the instance boots."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type."
  type        = string
}

variable "subnet_id" {
  description = "Public subnet of the instance's network interface."
  type        = string
}

variable "security_group_ids" {
  description = "Security groups of the instance's network interface."
  type        = list(string)
}

variable "permissions_boundary" {
  description = "ARN of the boundary policy bootstrap creates; the deploy role may create and edit host roles only with it."
  type        = string
}

variable "scope_policy" {
  description = "Inline IAM policy JSON naming the secrets and bucket prefixes this host may reach."
  type        = string
}

variable "compose_plugin_version" {
  description = "Docker Compose plugin release installed by user data."
  type        = string
}

variable "buildx_plugin_version" {
  description = "Docker buildx plugin release installed by user data; Compose builds through it."
  type        = string
}

variable "certbot_image" {
  description = "certbot image the renewal timer runs."
  type        = string
}
