/**
 * Terraform/OpenTofu module for Parsl Ephemeral AWS Provider - Bastion Host
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

locals {
  use_spot = var.use_spot_instances && var.spot_max_price != null
}

resource "aws_iam_role" "bastion_role" {
  name = "parsl-bastion-role-${var.workflow_id}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      },
    ]
  })

  managed_policy_arns = [
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  ]

  inline_policy {
    name = "BastionHostPolicy"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Effect = "Allow"
          Action = [
            "ec2:DescribeInstances",
            "ec2:DescribeInstanceStatus",
            "ec2:StartInstances",
            "ec2:StopInstances",
            "ec2:TerminateInstances",
            "ec2:CreateTags",
            "ec2:DescribeTags"
          ]
          Resource = "*"
        },
        {
          Effect = "Allow"
          Action = [
            "ssm:PutParameter",
            "ssm:GetParameter",
            "ssm:DeleteParameter"
          ]
          Resource = "arn:aws:ssm:${var.region}:*:parameter/parsl/workflows/${var.workflow_id}/*"
        }
      ]
    })
  }

  tags = merge(
    {
      Name            = "parsl-bastion-role-${var.workflow_id}"
      ParslResource   = "true"
      ParslWorkflowId = var.workflow_id
    },
    var.tags
  )
}

resource "aws_iam_instance_profile" "bastion_profile" {
  name = "parsl-bastion-profile-${var.workflow_id}"
  role = aws_iam_role.bastion_role.name
}

resource "aws_instance" "bastion" {
  count = local.use_spot ? 0 : 1

  ami                  = var.image_id
  instance_type        = var.instance_type
  subnet_id            = var.subnet_id
  security_groups      = [var.security_group_id]
  iam_instance_profile = aws_iam_instance_profile.bastion_profile.name
  key_name             = var.key_name == "" ? null : var.key_name
  user_data            = var.user_data == "" ? null : var.user_data

  instance_initiated_shutdown_behavior = "terminate"
  monitoring                           = true

  tags = merge(
    {
      Name            = "parsl-bastion-${var.workflow_id}"
      ParslResource   = "true"
      ParslWorkflowId = var.workflow_id
      ParslResourceType = "bastion"
      ParslIdleTimeout = tostring(var.idle_timeout)
    },
    var.tags
  )
}

resource "aws_spot_instance_request" "bastion_spot" {
  count = local.use_spot ? 1 : 0

  ami                  = var.image_id
  instance_type        = var.instance_type
  subnet_id            = var.subnet_id
  security_groups      = [var.security_group_id]
  iam_instance_profile = aws_iam_instance_profile.bastion_profile.name
  key_name             = var.key_name == "" ? null : var.key_name
  user_data            = var.user_data == "" ? null : var.user_data

  spot_price                      = var.spot_max_price
  wait_for_fulfillment            = true
  instance_interruption_behavior  = "terminate"
  spot_type                       = "persistent"
  instance_initiated_shutdown_behavior = "terminate"
  monitoring                      = true

  tags = merge(
    {
      Name              = "parsl-bastion-spot-${var.workflow_id}"
      ParslResource     = "true"
      ParslWorkflowId   = var.workflow_id
      ParslResourceType = "bastion"
      ParslIdleTimeout  = tostring(var.idle_timeout)
    },
    var.tags
  )
}
