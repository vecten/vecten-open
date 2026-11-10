data "aws_route53_zone" "this" {
  name         = var.dns_zone
  private_zone = false
}

resource "aws_route53_record" "hostname" {
  for_each = { for c in local.components : local.hostname_of[c] => local.host_of[c] }

  zone_id = data.aws_route53_zone.this.zone_id
  name    = each.key
  type    = "A"
  ttl     = 60
  records = [module.host[each.value].public_ip]
}
