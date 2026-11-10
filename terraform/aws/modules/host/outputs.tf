output "instance_id" {
  description = "EC2 instance id, the SSM Run Command target."
  value       = aws_instance.this.id
}

output "public_ip" {
  description = "The Elastic IP the hostnames resolve to."
  value       = aws_eip.this.public_ip
}

output "role_name" {
  description = "IAM role of the instance."
  value       = aws_iam_role.this.name
}

output "permissions_boundary" {
  description = "Permissions boundary of the instance's role."
  value       = aws_iam_role.this.permissions_boundary
}

output "instance_type" {
  description = "EC2 instance type."
  value       = aws_instance.this.instance_type
}

output "scope_policy" {
  description = "Inline scope policy JSON attached to the role."
  value       = aws_iam_role_policy.scope.policy
}

output "settings" {
  description = "Configuration the module tests assert: boot image, metadata options, root volume, managed policies, user data."
  value = {
    ami                         = aws_instance.this.ami
    http_tokens                 = aws_instance.this.metadata_options[0].http_tokens
    http_put_response_hop_limit = aws_instance.this.metadata_options[0].http_put_response_hop_limit
    root_volume = {
      volume_type = aws_instance.this.root_block_device[0].volume_type
      volume_size = aws_instance.this.root_block_device[0].volume_size
      encrypted   = aws_instance.this.root_block_device[0].encrypted
    }
    managed_policy_arns = [aws_iam_role_policy_attachment.ssm_core.policy_arn]
    user_data           = local.user_data
  }
}
