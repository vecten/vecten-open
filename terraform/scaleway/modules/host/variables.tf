variable "name" {
  description = "Name of the server, its flexible IP and its security group."
  type        = string
}

variable "project_id" {
  description = "Resources project of the server."
  type        = string
}

variable "zone" {
  description = "Zone of the server, its IP and its security group, <region>-1."
  type        = string
}

variable "instance_type" {
  description = "Scaleway commercial type, such as DEV1-M or POP2-2C-8G."
  type        = string
}

variable "private_network_id" {
  description = "Private network the server's private NIC joins."
  type        = string
}

variable "tags" {
  description = "Tags on the server, its IP and its security group."
  type        = list(string)
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

variable "watch_config" {
  description = "What the watcher reads from /etc/vecten/watch.json before any release exists: host key, components, artifacts bucket, region, resources project and secret path prefix."
  type = object({
    host          = string
    components    = list(string)
    bucket        = string
    region        = string
    project_id    = string
    secret_prefix = string
  })
}
