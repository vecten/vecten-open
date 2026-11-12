locals {
  ssm_core_policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    compose_plugin_version = var.compose_plugin_version
    buildx_plugin_version  = var.buildx_plugin_version
    certbot_image          = var.certbot_image
  })

  tags = {
    Name          = var.name
    "vecten:host" = var.host
  }
}

resource "aws_iam_role" "this" {
  name                 = var.name
  permissions_boundary = var.permissions_boundary

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.this.name
  policy_arn = local.ssm_core_policy_arn
}

resource "aws_iam_role_policy" "scope" {
  name   = "scope"
  role   = aws_iam_role.this.name
  policy = var.scope_policy
}

resource "aws_iam_instance_profile" "this" {
  name = var.name
  role = aws_iam_role.this.name

  tags = local.tags
}

# The Elastic IP sits on this interface before the instance boots, so user data reaches the
# internet from the first second without a NAT gateway.
resource "aws_network_interface" "this" {
  subnet_id       = var.subnet_id
  security_groups = var.security_group_ids

  tags = local.tags
}

resource "aws_eip" "this" {
  domain            = "vpc"
  network_interface = aws_network_interface.this.id

  tags = local.tags
}

resource "aws_instance" "this" {
  ami                  = var.ami_id
  instance_type        = var.instance_type
  iam_instance_profile = aws_iam_instance_profile.this.name
  user_data            = local.user_data

  primary_network_interface {
    network_interface_id = aws_network_interface.this.id
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_size_gb
    encrypted   = true
  }

  tags        = local.tags
  volume_tags = local.tags

  # The SSM parameter moves to every new AL2023 release; following it would replace the VM.
  lifecycle {
    ignore_changes = [ami]
  }

  depends_on = [aws_eip.this, aws_iam_role_policy.scope, aws_iam_role_policy_attachment.ssm_core]
}
