Prevent session adoption from overtaking an in-flight connection before its
session is published. Keep the connection reserved through cancellation cleanup
and release the reservation after failed launches.
