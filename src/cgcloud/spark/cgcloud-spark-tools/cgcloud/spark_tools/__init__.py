import logging
import re
import os
import fcntl
from grp import getgrnam
from pwd import getpwnam
import socket
from urllib2 import urlopen
from subprocess import check_call, call
import time
import itertools

from bd2k.util.files import mkdir_p
import boto.ec2
from bd2k.util import memoize

initctl = '/sbin/initctl'

sudo = '/usr/bin/sudo'

log = logging.getLogger( __name__ )


class SparkTools( object ):
    """
    Tools for master discovery and managing the slaves file for Hadoop and Spark.

    Master discovery works as follows: All instances in a Spark cluster are tagged with the
    instance ID of the master. Each instance will look up the private IP of 1) the master
    instance using the EC2 API (via boto) and 2) itself using the instance metadata endpoint.
    Generic names will be added for both IPs to /etc/hosts, namely spark-master and spark-node.
    All configuration files use these names instead of hard-coding the IPs. This is all that's
    needed to boot a working cluster.

    In order to facilitate the start-all.sh and stop-all.sh scripts in Hadoop and Spark,
    the slaves file needs to be populated as well. The master seeds the slaves file by listing
    all instances tagged with its own instance ID. Additionally, the slaves ssh into the master
    to have their own IP added to the master's slaves file, thereby enabling the dynamic addition
    of slaves to a cluster. Both actions are managed by the spark-manage-slaves script.

    The slaves file in spark/conf and hadoop/etc/hadoop is actually a symlink to a file in /tmp
    whose name ends in the IP of the master. This is to ensure that a fresh slaves file is used
    for every incarnation of the AMI and after each restart of the master instance.
    """

    def __init__( self, user, install_dir ):
        """
        :param user: the user the services run as
        :param install_dir: root installation directory, e.g. /opt
        """
        super( SparkTools, self ).__init__( )
        self.user = user
        self.install_dir = install_dir
        self.uid = getpwnam( self.user ).pw_uid
        self.gid = getgrnam( self.user ).gr_gid

    def start( self, lazy_dirs ):
        """
        Start the Hadoop and Spark services on this node

        :param lazy_dirs: directories to create, typically located on ephemeral volumes
        """
        while not os.path.exists( '/tmp/cloud-init.done' ):
            log.info( "Waiting for cloud-init to finish ..." )
            time.sleep( 1 )
        log.info( "Starting sparkbox" )
        self.__patch_etc_hosts( { 'spark-master': self.master_ip, 'spark-node': self.node_ip } )
        self.__create_var_dirs( lazy_dirs )
        if self.master_ip == self.node_ip:
            node_type = 'master'
            self.__publish_host_key( )
            self.__prepare_slaves_file( )
            self.__format_namenode( )
        else:
            node_type = 'slave'
            self.__get_master_host_key( )
            self.__wait_for_master_ssh( )
            self.__register_with_master( )
        log.info( "Starting %s services" % node_type )
        check_call( [ initctl, 'emit', 'sparkbox-start-%s' % node_type ] )

    def stop( self ):
        log.info( "Stopping sparkbox" )
        self.__patch_etc_hosts( { 'spark-master': None, 'spark-node': None } )

    def manage_slaves( self, slaves_to_add=None ):
        log.info( "Managing slaves file" )
        slaves_path = "/tmp/slaves-" + self.master_ip
        with open( slaves_path, 'a+' ) as f:
            fcntl.flock( f, fcntl.LOCK_EX )
            if slaves_to_add:
                log.info( "Adding slaves: %r", slaves_to_add )
                slaves = set( _.strip( ) for _ in f.readlines( ) )
                # format is IP : SSH_KEY_ALGO : SSH_HOST_KEY without the spaces
                slaves.update( _.split( ':' )[ 0 ] for _ in slaves_to_add )
            else:
                log.info( "Initializing slaves file" )
                reservations = self.ec2.get_all_reservations(
                    filters={ 'tag:spark_master': self.master_id } )
                slaves = set( i.private_ip_address
                    for r in reservations
                    for i in r.instances if i.id != self.master_id )
                log.info( "Found %i slave.", len( slaves ) )
            if '' in slaves: slaves.remove( '' )
            slaves = list( slaves )
            slaves.sort( )
            slaves.append( '' )
            f.seek( 0 )
            f.truncate( 0 )
            f.write( '\n'.join( slaves ) )
        if slaves_to_add:
            log.info( "Adding host keys for slaves" )
            self.__add_host_keys( slaves_to_add )

    @classmethod
    def instance_data( cls, path ):
        return urlopen( 'http://169.254.169.254/latest/' + path ).read( )

    @classmethod
    def meta_data( cls, path ):
        return cls.instance_data( 'meta-data/' + path )

    @classmethod
    def user_data( cls ):
        user_data = cls.instance_data( 'user-data' )
        log.info( "User data is '%s'", user_data )
        return user_data

    @property
    @memoize
    def node_ip( self ):
        ip = self.meta_data( 'local-ipv4' )
        log.info( "Local IP is '%s'", ip )
        return ip

    @property
    @memoize
    def instance_id( self ):
        instance_id = self.meta_data( 'instance-id' )
        log.info( "Instance ID is '%s'", instance_id )
        return instance_id

    @property
    @memoize
    def availability_zone( self ):
        zone = self.meta_data( 'placement/availability-zone' )
        log.info( "Availability zone is '%s'", zone )
        return zone

    @property
    @memoize
    def region( self ):
        m = re.match( r'^([a-z]{2}-[a-z]+-[1-9][0-9]*)([a-z])$', self.availability_zone )
        assert m
        region = m.group( 1 )
        log.info( "Region is '%s'", region )
        return region

    @property
    @memoize
    def ec2( self ):
        return boto.ec2.connect_to_region( self.region )

    @property
    @memoize
    def master_id( self ):
        while True:
            master_id = self.__get_instance_tag( self.instance_id, 'spark_master' )
            if master_id:
                log.info( "Master's instance ID is '%s'", master_id )
                return master_id
            log.warn( "Instance not tagged with master's instance ID, retrying" )
            time.sleep( 5 )

    @property
    @memoize
    def master_ip( self ):
        if self.master_id == self.instance_id:
            master_ip = self.node_ip
            log.info( "I am the master" )
        else:
            log.info( "I am a slave" )
            reservations = self.ec2.get_all_reservations( instance_ids=[ self.master_id ] )
            instances = (i for r in reservations for i in r.instances if i.id == self.master_id)
            master_instance = next( instances )
            assert next( instances, None ) is None
            master_ip = master_instance.private_ip_address
        log.info( "Master IP is '%s'", master_ip )
        return master_ip

    def __get_master_host_key( self ):
        log.info( "Getting master's host key" )
        master_host_key = self.__get_instance_tag( self.master_id, 'ssh_host_key' )
        if master_host_key:
            self.__add_host_keys( [ 'spark-master:' + master_host_key ] )
        else:
            log.warn( "Could not get master's host key" )

    def __add_host_keys( self, host_keys, globally=None ):
        if globally is None:
            globally = os.geteuid( ) == 0
        if globally:
            known_hosts_path = '/etc/ssh/ssh_known_hosts'
        else:
            known_hosts_path = os.path.expanduser( '~/.ssh/known_hosts' )
        with open( known_hosts_path, 'a+' ) as f:
            fcntl.flock( f, fcntl.LOCK_EX )
            keys = set( _.strip( ) for _ in f.readlines( ) )
            keys.update( ' '.join( _.split( ':' ) ) for _ in host_keys )
            if '' in keys: keys.remove( '' )
            keys = list( keys )
            keys.sort( )
            keys.append( '' )
            f.seek( 0 )
            f.truncate( 0 )
            f.write( '\n'.join( keys ) )

    def __wait_for_master_ssh( self ):
        """
        Wait until the instance represented by this box is accessible via SSH.
        """
        for i in itertools.count( ):
            s = socket.socket( socket.AF_INET, socket.SOCK_STREAM )
            try:
                s.settimeout( 5 )
                s.connect( ('spark-master', 22) )
                return
            except socket.error:
                pass
            finally:
                s.close( )

    def __register_with_master( self ):
        log.info( "Registering with master" )
        for tries in range( 5 ):
            status_code = call(
                # '-o', 'UserKnownHostsFile=/dev/null','-o', 'StrictHostKeyChecking=no'
                [ sudo, '-u', self.user, 'ssh', 'spark-master', 'sparkbox-manage-slaves',
                    self.node_ip + ":" + self.__get_host_key( ) ] )
            if 0 == status_code: return
            log.warn( "ssh returned %i, retrying in 5s", status_code )
            time.sleep( 5 )
        raise RuntimeError( "Failed to register with master" )

    def __get_host_key( self ):
        with open( '/etc/ssh/ssh_host_ecdsa_key.pub' ) as f:
            return ':'.join( f.read( ).split( )[ :2 ] )

    def __publish_host_key( self ):
        master_host_key = self.__get_host_key( )
        self.ec2.create_tags( self.master_id, dict( ssh_host_key=master_host_key ) )

    def __create_var_dirs( self, dirs ):
        log.info( "Creating directory structure" )
        for path in dirs:
            mkdir_p( path )
            os.chown( path, self.uid, self.gid )

    def __prepare_slaves_file( self ):
        log.info( "Preparing slaves file" )
        tmp_slaves = "/tmp/slaves-" + self.master_ip
        open( tmp_slaves, "a" ).close( )
        os.chown( tmp_slaves, self.uid, self.gid )
        self.__symlink( self.install_dir + "/hadoop/etc/hadoop/slaves", tmp_slaves )
        self.__symlink( self.install_dir + "/spark/conf/slaves", tmp_slaves )

    def __format_namenode( self ):
        log.info( "Formatting namenode" )
        call( [ 'sudo', '-u', self.user,
            self.install_dir + '/hadoop/bin/hdfs', 'namenode', '-format', '-nonInteractive' ] )

    def __patch_etc_hosts( self, hosts ):
        log.info( "Patching /etc/host" )
        # FIXME: The handling of /etc/hosts isn't atomic
        with open( '/etc/hosts', 'r+' ) as etc_hosts:
            lines = [ line
                for line in etc_hosts.readlines( )
                if not any( host in line for host in hosts.iterkeys( ) ) ]
            for host, ip in hosts.iteritems( ):
                if ip: lines.append( "%s %s\n" % ( ip, host ) )
            etc_hosts.seek( 0 )
            etc_hosts.truncate( 0 )
            etc_hosts.writelines( lines )

    def __symlink( self, symlink, target ):
        if os.path.lexists( symlink ): os.unlink( symlink )
        os.symlink( target, symlink )

    def __get_instance_tag( self, instance_id, key ):
        """
        :rtype: str
        """
        tags = self.ec2.get_all_tags( filters={ 'resource-id': instance_id, 'key': key } )
        return tags[ 0 ].value if tags else None

