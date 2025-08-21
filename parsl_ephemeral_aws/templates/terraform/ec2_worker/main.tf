/**
 * Terraform/OpenTofu module for Parsl Ephemeral AWS Provider - EC2 Worker Resources
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

resource "aws_launch_template" "worker_launch_template" {
  name = "parsl-worker-${var.workflow_id}-${var.block_id}"
  description = "Launch template for Parsl worker nodes in workflow ${var.workflow_id}, block ${var.block_id}"

  image_id = var.image_id
  instance_type = var.instance_type
  key_name = var.key_name == "" ? null : var.key_name
  user_data = var.user_data == "" ? null : base64encode(var.user_data)

  vpc_security_group_ids = [var.security_group_id]

  dynamic "iam_instance_profile" {
    for_each = var.instance_profile == "" ? [] : [1]
    content {
      arn = var.instance_profile
    }
  }

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags = merge(
      {
        Name            = "parsl-worker-${var.workflow_id}-${var.block_id}"
        ParslResource   = "true"
        ParslWorkflowId = var.workflow_id
        ParslBlockId    = var.block_id
        ParslResourceType = "worker"
      },
      var.tags
    )
  }
}

resource "aws_instance" "worker_nodes" {
  count = local.use_spot ? 0 : var.nodes_per_block

  subnet_id = var.subnet_id

  launch_template {
    id = aws_launch_template.worker_launch_template.id
    version = aws_launch_template.worker_launch_template.latest_version
  }

  tags = merge(
    {
      Name            = "parsl-worker-${var.workflow_id}-${var.block_id}-${count.index}"
      ParslResource   = "true"
      ParslWorkflowId = var.workflow_id
      ParslBlockId    = var.block_id
      ParslNodeIndex  = tostring(count.index)
    },
    var.tags
  )
}

# For spot instances, we use the EC2 Fleet API
resource "aws_iam_role" "spot_fleet_role" {
  count = local.use_spot ? 1 : 0

  name = "parsl-spot-fleet-role-${var.workflow_id}-${var.block_id}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "spotfleet.amazonaws.com"
        }
      },
    ]
  })

  managed_policy_arns = [
    "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
  ]

  tags = merge(
    {
      ParslResource   = "true"
      ParslWorkflowId = var.workflow_id
      ParslBlockId    = var.block_id
    },
    var.tags
  )
}

resource "aws_ec2_fleet" "spot_worker_fleet" {
  count = local.use_spot ? 1 : 0

  launch_template_config {
    launch_template_specification {
      launch_template_id = aws_launch_template.worker_launch_template.id
      version = aws_launch_template.worker_launch_template.latest_version
    }

    override {
      subnet_id = var.subnet_id
    }
  }

  target_capacity_specification {
    default_target_capacity_type = "spot"
    total_target_capacity = var.nodes_per_block
  }

  spot_options {
    allocation_strategy = "lowestPrice"
    maintenance_strategies {
      capacity_rebalance {
        replacement_strategy = "launch"
      }
    }
  }

  type = "maintain"

  tags = merge(
    {
      Name            = "parsl-spot-fleet-${var.workflow_id}-${var.block_id}"
      ParslResource   = "true"
      ParslWorkflowId = var.workflow_id
      ParslBlockId    = var.block_id
    },
    var.tags
  )

  depends_on = [aws_iam_role.spot_fleet_role]
}
