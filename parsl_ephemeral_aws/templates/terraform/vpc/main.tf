/**
 * Terraform/OpenTofu module for Parsl Ephemeral AWS Provider - VPC Network Resources
 *
 * SPDX-License-Identifier: Apache-2.0
 * SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
 */

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.0.0"
}

resource "aws_vpc" "parsl_vpc" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(
    {
      Name            = "parsl-vpc-${var.workflow_id}"
      ParslResource   = "true"
      ParslWorkflowId = var.workflow_id
    },
    var.tags
  )
}

resource "aws_internet_gateway" "parsl_igw" {
  vpc_id = aws_vpc.parsl_vpc.id

  tags = merge(
    {
      Name            = "parsl-igw-${var.workflow_id}"
      ParslResource   = "true"
      ParslWorkflowId = var.workflow_id
    },
    var.tags
  )
}

resource "aws_subnet" "parsl_subnet" {
  vpc_id                  = aws_vpc.parsl_vpc.id
  cidr_block              = var.subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = var.use_public_ips

  tags = merge(
    {
      Name            = "parsl-subnet-${var.workflow_id}"
      ParslResource   = "true"
      ParslWorkflowId = var.workflow_id
    },
    var.tags
  )
}

resource "aws_route_table" "parsl_route_table" {
  vpc_id = aws_vpc.parsl_vpc.id

  tags = merge(
    {
      Name            = "parsl-rt-${var.workflow_id}"
      ParslResource   = "true"
      ParslWorkflowId = var.workflow_id
    },
    var.tags
  )
}

resource "aws_route" "internet_route" {
  route_table_id         = aws_route_table.parsl_route_table.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.parsl_igw.id
  depends_on             = [aws_internet_gateway.parsl_igw]
}

resource "aws_route_table_association" "parsl_route_association" {
  subnet_id      = aws_subnet.parsl_subnet.id
  route_table_id = aws_route_table.parsl_route_table.id
}

resource "aws_security_group" "parsl_sg" {
  name        = "parsl-sg-${var.workflow_id}"
  description = "Security group for Parsl workflow ${var.workflow_id}"
  vpc_id      = aws_vpc.parsl_vpc.id

  # SSH access
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "SSH access"
  }

  # Parsl communication ports
  ingress {
    from_port   = 54000
    to_port     = 55000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Parsl communication ports"
  }

  # Allow all outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound traffic"
  }

  tags = merge(
    {
      Name            = "parsl-sg-${var.workflow_id}"
      ParslResource   = "true"
      ParslWorkflowId = var.workflow_id
    },
    var.tags
  )
}

# Self-referential rule for allowing all traffic within the security group
resource "aws_security_group_rule" "parsl_sg_self" {
  security_group_id        = aws_security_group.parsl_sg.id
  type                     = "ingress"
  from_port                = 0
  to_port                  = 0
  protocol                 = "-1"
  source_security_group_id = aws_security_group.parsl_sg.id
  description              = "Allow all traffic within security group"
}