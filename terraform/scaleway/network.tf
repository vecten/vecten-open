# One network for the hosts and the database instances. Security groups filter public traffic
# only, so inside this network every host reaches every instance; the database credentials are
# the gate.
resource "scaleway_vpc_private_network" "this" {
  name       = local.name_prefix
  project_id = var.project_id
  region     = var.region
  tags       = local.tags
}
