data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  zones = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "this" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = local.name_prefix
  }

  lifecycle {
    precondition {
      condition     = local.region == var.region
      error_message = "The AWS provider is configured for a region other than var.region; configure it with var.region."
    }
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = local.name_prefix
  }
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.this.id
  availability_zone       = local.zones[count.index]
  cidr_block              = cidrsubnet(aws_vpc.this.cidr_block, 8, count.index)
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name_prefix}-public-${count.index}"
  }
}

# Private subnets have no route out: RDS needs none, and the network has no NAT gateway.
resource "aws_subnet" "private" {
  count = 2

  vpc_id                  = aws_vpc.this.id
  availability_zone       = local.zones[count.index]
  cidr_block              = cidrsubnet(aws_vpc.this.cidr_block, 8, 10 + count.index)
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name_prefix}-private-${count.index}"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = {
    Name = "${local.name_prefix}-public"
  }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "hosts" {
  name        = "${local.name_prefix}-hosts"
  description = "Vecten hosts: HTTP for the certificate challenge and redirect, HTTPS"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTP"
    protocol    = "tcp"
    from_port   = 80
    to_port     = 80
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All outbound"
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-hosts"
  }
}

# Rules are separate resources so each one stays known at plan time for the tests.
resource "aws_security_group" "database" {
  name        = "${local.name_prefix}-database"
  description = "Vecten Postgres: 5432 from the hosts, and from explicit CIDRs only when opted in"
  vpc_id      = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-database"
  }
}

resource "aws_vpc_security_group_ingress_rule" "database_from_hosts" {
  security_group_id            = aws_security_group.database.id
  description                  = "Postgres from the hosts"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.hosts.id
}

resource "aws_vpc_security_group_ingress_rule" "database_public" {
  for_each = toset(var.database_public_cidrs)

  security_group_id = aws_security_group.database.id
  description       = "Postgres from an allow-listed CIDR"
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
  cidr_ipv4         = each.value
}
