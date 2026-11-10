resource "scaleway_domain_record" "hostname" {
  for_each = { for c in local.components : local.hostname_of[c] => local.host_of[c] }

  dns_zone   = var.dns_zone
  name       = trimsuffix(each.key, ".${var.dns_zone}")
  type       = "A"
  ttl        = 60
  data       = module.host[each.value].public_ip
  project_id = local.dns_project_id
}
