/**
 * Outputs for VPC Network Resources
 *
 * SPDX-License-Identifier: Apache-2.0
 * SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
 */

output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.parsl_vpc.id
}

output "subnet_id" {
  description = "ID of the subnet"
  value       = aws_subnet.parsl_subnet.id
}

output "security_group_id" {
  description = "ID of the security group"
  value       = aws_security_group.parsl_sg.id
}

output "route_table_id" {
  description = "ID of the route table"
  value       = aws_route_table.parsl_route_table.id
}

output "internet_gateway_id" {
  description = "ID of the internet gateway"
  value       = aws_internet_gateway.parsl_igw.id
}