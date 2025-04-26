"""Logging utilities for Parsl Ephemeral AWS Provider.

SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors
"""

import logging
import os
import sys
from typing import Optional, Dict, Any, Union


def configure_logger(
    logger_name: str = 'parsl_ephemeral_aws',
    level: int = logging.INFO,
    log_format: Optional[str] = None,
    file_path: Optional[str] = None,
    stream: Optional[Any] = sys.stdout,
    clear_handlers: bool = False
) -> logging.Logger:
    """Configure a logger for the Parsl Ephemeral AWS Provider.
    
    Parameters
    ----------
    logger_name : str, optional
        Name of the logger, by default 'parsl_ephemeral_aws'
    level : int, optional
        Logging level, by default logging.INFO
    log_format : Optional[str], optional
        Logging format, by default None (uses a default format)
    file_path : Optional[str], optional
        Path to log file, by default None (no file logging)
    stream : Optional[Any], optional
        Stream to log to, by default sys.stdout
    clear_handlers : bool, optional
        Whether to clear existing handlers, by default False
        
    Returns
    -------
    logging.Logger
        Configured logger
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    
    # Clear existing handlers if requested
    if clear_handlers:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
    
    # Set default format if not provided
    if log_format is None:
        log_format = '[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s'
    
    formatter = logging.Formatter(log_format)
    
    # Add stream handler if requested
    if stream:
        stream_handler = logging.StreamHandler(stream)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    
    # Add file handler if requested
    if file_path:
        # Ensure directory exists
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        
        file_handler = logging.FileHandler(file_path)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def set_all_loggers_level(level: int) -> None:
    """Set the log level for all loggers in the Parsl Ephemeral AWS Provider.
    
    Parameters
    ----------
    level : int
        Logging level to set
    """
    # Main logger
    logging.getLogger('parsl_ephemeral_aws').setLevel(level)
    
    # Submodule loggers
    for module in ['modes', 'compute', 'network', 'state', 'utils']:
        logging.getLogger(f'parsl_ephemeral_aws.{module}').setLevel(level)


def get_boto3_clients_logger() -> logging.Logger:
    """Get the logger for boto3 clients.
    
    Returns
    -------
    logging.Logger
        Boto3 clients logger
    """
    return logging.getLogger('botocore.client')


def set_boto3_log_level(level: int) -> None:
    """Set the log level for boto3 loggers.
    
    Parameters
    ----------
    level : int
        Logging level to set
    """
    # Boto3 loggers
    logging.getLogger('boto3').setLevel(level)
    logging.getLogger('botocore').setLevel(level)
    logging.getLogger('s3transfer').setLevel(level)
    logging.getLogger('urllib3').setLevel(level)


def configure_provider_logging(
    provider: Any,
    level: int = logging.INFO,
    include_boto3: bool = False,
    log_file: Optional[str] = None
) -> None:
    """Configure logging for a provider instance.
    
    Parameters
    ----------
    provider : EphemeralAWSProvider
        Provider instance
    level : int, optional
        Logging level, by default logging.INFO
    include_boto3 : bool, optional
        Whether to configure boto3 logging, by default False
    log_file : Optional[str], optional
        Path to log file, by default None
    """
    # Create a log file path if requested but not provided
    if log_file is True:
        log_dir = os.path.expanduser('~/.parsl/logs')
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f'parsl_aws_{provider.workflow_id}.log')
    
    # Configure main logger
    configure_logger(
        logger_name='parsl_ephemeral_aws',
        level=level,
        file_path=log_file if isinstance(log_file, str) else None
    )
    
    # Set level for all submodule loggers
    set_all_loggers_level(level)
    
    # Configure boto3 logging if requested
    if include_boto3:
        set_boto3_log_level(level)