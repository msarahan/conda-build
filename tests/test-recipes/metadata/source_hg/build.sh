# We test the environment variables in a different recipe

# Ensure we are in a hg repo
[ -d .hg ]
hg id
[ "$(hg id)" = "0d15c0b5fc78 (some-branch) tip" ]
