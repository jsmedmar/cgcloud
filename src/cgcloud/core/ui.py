from collections import OrderedDict
from importlib import import_module
import logging
import os
import sys

from cgcloud.lib.util import Application, app_name, UserError

import cgcloud.core

log = logging.getLogger( __name__ )

PACKAGES = [ cgcloud.core ] + [ import_module( package_name )
    for package_name in os.environ.get( 'CGCLOUD_PLUGINS', "" ).split( ":" )
    if package_name ]


def try_main( args=None ):
    app = CGCloud( )
    for package in PACKAGES:
        for command in package.COMMANDS:
            app.add( command )
    app.run( args )


def main( args=None ):
    try:
        try_main( args )
    except UserError as e:
        log.error( e.message )


class CGCloud( Application ):
    debug_log_file_name = '%s.{pid}.log' % app_name( )

    def __init__( self ):
        super( CGCloud, self ).__init__( )
        self.option( '--debug',
                     default=False, action='store_true',
                     help='Write debug log to %s in current directory.' % self.debug_log_file_name )
        self.boxes = OrderedDict( )
        for package in PACKAGES:
            for box_cls in package.BOXES:
                self.boxes[ box_cls.role( ) ] = box_cls

    def prepare( self, options ):
        root_logger = logging.getLogger( )
        if len( root_logger.handlers ) == 0:
            stream_handler = logging.StreamHandler( sys.stderr )
            stream_handler.setFormatter( logging.Formatter( "%(levelname)s: %(message)s" ) )
            stream_handler.setLevel( logging.INFO )
            if options.debug:
                root_logger.setLevel( logging.DEBUG )
                file_name = self.debug_log_file_name.format( pid=os.getpid( ) )
                file_handler = logging.FileHandler( file_name )
                file_handler.setLevel( logging.DEBUG )
                file_handler.setFormatter( logging.Formatter(
                    '%(asctime)s: %(levelname)s: %(name)s: %(message)s' ) )
                root_logger.addHandler( file_handler )
