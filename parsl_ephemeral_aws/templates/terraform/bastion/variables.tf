/**
 * Variables for Bastion Host
 *
 * SPDX-License-Identifier: Apache-2.0
 * SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
 */

variable "workflow_id" {
  description = "Unique ID for the workflow"
  type        = string
}

variable "region" {
  description = "AWS region to use"
  type        = string
}

variable "vpc_id" {
  description = "ID of the VPC to use"
  type        = string
}

variable "subnet_id" {
  description = "ID of the subnet to use"
  type        = string
}

variable "security_group_id" {
  description = "ID of the security group to use"
  type        = string
}

variable "image_id" {
  description = "AMI ID to use for the bastion host"
  type        = string
}

variable "instance_type" {
  description = "Instance type for the bastion host"
  type        = string
  default     = "t3.micro"
}

variable "key_name" {
  description = "SSH key pair name for the bastion host"
  type        = string
  default     = ""
}

variable "user_data" {
  description = "User data script for the bastion host"
  type        = string
  default     = ""
}

variable "use_spot_instances" {
  description = "Whether to use spot instances"
  type        = bool
  default     = false
}

variable "spot_max_price" {
  description = "Maximum price for spot instances (null for on-demand price)"
  type        = string
  default     = null
}

variable "idle_timeout" {
  description = "Minutes to wait before shutting down if idle"
  type        = number
  default     = 30
}

variable "tags" {
  description = "Additional tags to apply to resources"
  type        = map(string)
  default     = {}
}