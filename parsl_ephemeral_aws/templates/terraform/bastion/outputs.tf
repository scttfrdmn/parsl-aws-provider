/**
 * Outputs for Bastion Host
 *
 * SPDX-License-Identifier: Apache-2.0
 * SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
 */

output "bastion_host_id" {
  description = "ID of the bastion host"
  value       = length(aws_instance.bastion) > 0 ? aws_instance.bastion[0].id : (length(aws_spot_instance_request.bastion_spot) > 0 ? aws_spot_instance_request.bastion_spot[0].spot_instance_id : null)
}

output "bastion_host_role" {
  description = "Name of the IAM role for the bastion host"
  value       = aws_iam_role.bastion_role.name
}

output "bastion_host_role_arn" {
  description = "ARN of the IAM role for the bastion host"
  value       = aws_iam_role.bastion_role.arn
}

output "bastion_host_public_ip" {
  description = "Public IP of the bastion host"
  value       = length(aws_instance.bastion) > 0 ? aws_instance.bastion[0].public_ip : (length(aws_spot_instance_request.bastion_spot) > 0 ? aws_spot_instance_request.bastion_spot[0].public_ip : null)
}

output "bastion_host_private_ip" {
  description = "Private IP of the bastion host"
  value       = length(aws_instance.bastion) > 0 ? aws_instance.bastion[0].private_ip : (length(aws_spot_instance_request.bastion_spot) > 0 ? aws_spot_instance_request.bastion_spot[0].private_ip : null)
}
