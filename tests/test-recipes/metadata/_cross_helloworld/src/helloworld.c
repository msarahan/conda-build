#include <libgreeting.h>
#include <stdio.h>

int main(int argc, char * argv[])
{
    char * say_it = greeting(GREETING_SUFFIX);
    if (say_it != NULL)
    {
        puts(say_it);
        putc('\n');
        return 0;
    }
    return 1;
}
