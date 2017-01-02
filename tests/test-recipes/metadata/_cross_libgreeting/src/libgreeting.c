#include <string.h>

#define SALUTATION "Hello World! "

char * greeting(char const * suffix)
{
    char * res = malloc(strlen(SALUTATION) + strlen(suffix) + 1);
    if (res == NULL)
        return NULL;
    res[0] = '\0';
    strcat(res, SALUTATION);
    strcat(res, suffix);
    return res;
}
